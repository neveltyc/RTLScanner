//! The transitive walk: fan-in, fan-out, and the path between two nets.
//!
//! The database stores one hop per row and says the closure is the consumer's.
//! This is that closure. Breadth first, so a node's depth is the fewest hops
//! that reach it rather than whichever route happened to be taken; the visited
//! set carries the bit window and the call context, because the same net
//! reached through a different call or about different bits is a different
//! question.
//!
//! What it will not do is invent reachability. A window crosses an arc only
//! where the correspondence is exact at every step, and widens to the whole
//! object otherwise; a call's rows are followed only alongside that call's
//! other rows. Both rules trade a narrower answer for one that cannot be
//! wrong about where a value came from.

use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use designdb::bits::BitSpan;
use designdb::resolve::{Anchor, ResolvedSignal};
use designdb::{Connection, Db, Direction, bits, schema};

/// How many nets a walk may reach before it stops and says so.
///
/// Not a display bound. `--limit` clips the answer and leaves the counts true,
/// which costs the whole walk; this bounds the walk itself, because no rule
/// about conditions makes a genuinely large value cone smaller. A design holds
/// structures — a wide crossbar, an array of FIFOs — whose value cone really is
/// most of it, and leaving conditions out entirely does not shrink one.
///
/// A hundred thousand nets is a few hundred megabytes and a few seconds, which
/// is as long as one question is worth. `RTLSCANNER_MAX_NODES` overrides it;
/// `0` removes it, and the caller carries the risk.
pub const MAX_NODES: usize = 100_000;

/// The walk's node budget, from the environment or the constant above.
///
/// Read once, before any command runs: a value that is not a number is a
/// mistake in the invocation, and finding that out after a twelve-second walk
/// would be the worst moment to learn it.
pub fn max_nodes() -> Result<Option<usize>, String> {
    let Some(set) = std::env::var_os("RTLSCANNER_MAX_NODES") else { return Ok(Some(MAX_NODES)) };
    let text = set.to_string_lossy().trim().to_string();
    match text.parse::<usize>() {
        Ok(0) => Ok(None),
        Ok(n) => Ok(Some(n)),
        Err(_) => Err(format!(
            "RTLSCANNER_MAX_NODES is '{text}', which is not a number of nets; \
             unset it for the default of {MAX_NODES}, or set 0 to remove the bound"
        )),
    }
}

/// Why a walk stopped without an answer.
#[derive(Debug, Clone)]
pub enum WalkError {
    /// The database could not answer something the walk asked it.
    Db(String),
    /// The cone passed the node budget. Reported rather than clipped: a walk
    /// that stopped early cannot say how big the cone is, and every count this
    /// tool reports is the true one.
    TooLarge {
        max_nodes: usize,
        /// Nets reached when it stopped.
        nodes: usize,
        /// The deepest level the walk finished, and its size — which is an
        /// answer the caller can actually ask for.
        depth: u32,
        depth_nodes: usize,
        depth_edges: usize,
    },
}

impl From<String> for WalkError {
    fn from(message: String) -> WalkError {
        WalkError::Db(message)
    }
}

/// How far a gating condition is followed.
///
/// A gate is a fact about the statement it gates — `en` decided whether this
/// assignment happened — and `trace` reports it that way: `gates[]` names the
/// condition and does not go on to ask where the condition came from. Following
/// it transitively makes a cone something else entirely: on real RTL the
/// conditions form one connected component (reset gates the state machine, the
/// state machine gates the enables, the enables gate the reset logic), so a
/// walk that follows them returns that component — the same answer for every
/// signal in it, and therefore no answer about any of them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Gating {
    /// Left out: only where the value came from.
    None,
    /// One hop from every net the value cone reaches: what gates each
    /// assignment along the way, and not where those gates came from.
    Direct,
    /// Followed like any other dependency, which is what it was until this
    /// proved to answer the same thing everywhere.
    Full,
}

impl Gating {
    pub fn tag(self) -> &'static str {
        match self {
            Gating::None => "none",
            Gating::Direct => "direct",
            Gating::Full => "full",
        }
    }
}

/// How far a walk may go, and what counts as the end of the road.
#[derive(Debug, Clone, Copy)]
pub struct Bounds {
    /// Hops from the start. `None` runs to the edges of the design.
    pub max_depth: Option<u32>,
    /// Stop at state elements: the answer is then this cycle's logic.
    pub comb: bool,
    /// Cross a latch anyway. A latch is transparent while its enable holds, so
    /// a glitch, a combinational loop closing through it, or a pulse-latch
    /// borrow all live on the far side of one.
    pub through_latch: bool,
    /// How far the conditions gating a statement are followed.
    pub gating: Gating,
    /// Nets the walk may reach before it gives up. `None` removes the bound.
    pub max_nodes: Option<usize>,
}

/// One net the walk reached.
#[derive(Debug, Clone)]
pub struct Node {
    pub net: i64,
    pub path: String,
    /// Fewest hops from the start. Breadth-first order makes the first arrival
    /// the shortest, so this needs no revisiting.
    pub depth: u32,
    pub width: Option<i64>,
    /// The net's own declared range, which is what a bit offset is spelled
    /// against. `None` for a type that has no single one — an aggregate.
    pub decl: Option<(i64, i64)>,
    /// Written by a procedure that runs on an edge.
    pub clocked: bool,
    /// Written by a procedure that runs on a level.
    pub latch: bool,
}

/// One arc between two nets the walk kept.
#[derive(Debug, Clone)]
pub struct Edge {
    pub source: i64,
    pub target: i64,
    /// Folded kind, in `trace`'s vocabulary.
    pub kind: crate::trace::HopKind,
    pub raw_kind: String,
    /// The value passes through a hierarchy boundary here.
    pub boundary: bool,
    /// This arc gates its target rather than supplying its value.
    pub control: bool,
    /// The statement behind it runs on an edge.
    pub clocked: bool,
    pub src_bits: BitSpan,
    pub tgt_bits: BitSpan,
    /// Whether the two ends correspond bit for bit.
    pub map_exact: Option<bool>,
    /// The far end is a state element and a combinational walk stopped here.
    /// Reported so a cone that ends can say where, rather than only that it
    /// ended.
    pub ends_at_state: bool,
    /// A constant condition rules this arc's statement out at this
    /// parameterisation. Kept, because the statement is in the design, and
    /// marked, because `trace` says so about the same row and a cone that did
    /// not would report logic this build cannot reach as an ordinary
    /// dependency.
    pub unreachable: bool,
    pub stmt_id: Option<i64>,
    pub call_site_id: Option<i64>,
    pub file: Option<Rc<str>>,
    pub line: Option<u32>,
    /// Hops from the start at which this arc was crossed.
    pub depth: u32,
}

impl Edge {
    /// What makes this arc distinct as an answer. Two rows of one statement
    /// touching different bits are two arcs; the same row met twice is one.
    fn key(&self) -> (i64, i64, Option<i64>, BitSpan, BitSpan) {
        (self.source, self.target, self.stmt_id, self.src_bits, self.tgt_bits)
    }
}

pub struct Cone {
    pub start: String,
    pub direction: Direction,
    pub bounds: Bounds,
    pub nodes: Vec<Node>,
    pub edges: Vec<Edge>,
    /// Nets whose window was widened to the whole object because a narrower
    /// one could not be carried across an arc.
    pub widened: usize,
    /// Nets the walk reached that have an arc it could not follow, because the
    /// export named the far end only by a reference it did not resolve.
    /// `trace` names those references; a cone cannot, and counting them is how
    /// a short answer says it is short rather than complete.
    pub unresolved: Vec<i64>,
}

/// The bits of one net a walk has already asked about.
///
/// Growing it is what ends the walk: a site is explored only when it asks
/// about a bit not yet covered, and every exploration covers at least one
/// more. A net is therefore visited at most as many times as it has bits, and
/// in practice as many times as it has distinct stored windows.
#[derive(Default)]
struct Cover {
    ranges: Vec<(u64, u64)>,
}

impl Cover {
    fn holds(&self, w: (u64, u64)) -> bool {
        // Contained in one range, the ranges being kept merged.
        self.ranges.iter().any(|(a, b)| *a <= w.0 && w.1 <= *b)
    }

    fn add(&mut self, w: (u64, u64)) {
        self.ranges.push(w);
        self.ranges.sort();
        let mut merged: Vec<(u64, u64)> = Vec::with_capacity(self.ranges.len());
        for (lo, hi) in self.ranges.drain(..) {
            match merged.last_mut() {
                // Touching counts as adjacent: [0,3] and [4,7] are one range,
                // or a window asking about both would never be held.
                Some(last) if lo <= last.1.saturating_add(1) => last.1 = last.1.max(hi),
                _ => merged.push((lo, hi)),
            }
        }
        self.ranges = merged;
    }
}

/// The whole of an object, as a window. Rebasing never produces offsets near
/// this, so it cannot collide with a real one.
const WHOLE: (u64, u64) = (0, u64::MAX);

/// One net, asked about at one width, in one call.
#[derive(Debug, Clone)]
struct Site {
    net: i64,
    /// The innermost expansion this site's rows belong to, or `None` at module
    /// level.
    ctx: Option<i64>,
    window: (u64, u64),
    depth: u32,
}

/// A net's design path, its width, and the range its bits are spelled against.
type Spelling = (String, Option<i64>, Option<(i64, i64)>);

/// What the walk needs to know about a net, asked once each.
#[derive(Default)]
struct Facts {
    /// Nets a state element writes, on either side of a whole-width port. Read
    /// once for the database, on first use: the relation does not depend on the
    /// walk, and a cone asks about every net it reaches.
    state_nets: Option<(HashSet<i64>, HashSet<i64>)>,
    /// Branches a constant condition rules out, with everything below them.
    dead_branches: Option<HashSet<i64>>,
    body_local: HashMap<i64, bool>,
    dep: HashMap<i64, Option<(String, Option<i64>)>>,
    /// Statements, by id. A cone crosses many more arcs than a design has
    /// statements — one `always` block answers for hundreds of them.
    stmt: HashMap<i64, Option<schema::StatementRow>>,
    /// Which expansion encloses which. A body walked once per call is told
    /// apart by this chain, and entering a nested call means arriving at a
    /// site whose parent is where the walk already is.
    call_parent: Option<HashMap<i64, Option<i64>>>,
    path: HashMap<i64, Spelling>,
    /// Source paths, shared rather than copied per edge: a module's every
    /// statement names one file, and a cone crosses thousands of statements.
    files: HashMap<String, Rc<str>>,
    /// Instance paths, which most nets share: a module with fifty nets has one
    /// path, walked up the tree once.
    scope: HashMap<i64, String>,
    /// Whether a procedure runs on an edge. Asked once per procedure rather
    /// than once per arc: a cone crosses thousands of arcs and a design has
    /// hundreds of procedures.
    clocked: HashMap<i64, bool>,
}

impl Facts {
    fn state_element(&mut self, c: &Connection, net: i64) -> Result<(bool, bool), String> {
        let (clocked, latch) = match &self.state_nets {
            Some(known) => known,
            None => self.state_nets.insert(schema::state_elements(c)?),
        };
        Ok((clocked.contains(&net), latch.contains(&net)))
    }

    /// Whether a constant condition rules this statement out.
    fn unreachable(
        &mut self,
        c: &Connection,
        stmt: Option<&schema::StatementRow>,
    ) -> Result<bool, String> {
        let Some(branch) = stmt.and_then(|s| s.branch_id) else { return Ok(false) };
        let dead = match &self.dead_branches {
            Some(known) => known,
            None => self.dead_branches.insert(schema::dead_branches(c)?),
        };
        Ok(dead.contains(&branch))
    }

    fn file(&mut self, path: &str) -> Rc<str> {
        if let Some(known) = self.files.get(path) {
            return known.clone();
        }
        let shared: Rc<str> = Rc::from(path);
        self.files.insert(path.to_string(), shared.clone());
        shared
    }

    /// The enclosing expansion of each call, read once.
    fn call_parent(&mut self, c: &Connection) -> Result<&HashMap<i64, Option<i64>>, String> {
        if self.call_parent.is_none() {
            self.call_parent = Some(schema::call_parents(c)?);
        }
        Ok(self.call_parent.as_ref().expect("just filled"))
    }

    fn dep_kind(
        &mut self,
        c: &Connection,
        dep_id: i64,
    ) -> Result<Option<(String, Option<i64>)>, String> {
        if let Some(known) = self.dep.get(&dep_id) {
            return Ok(known.clone());
        }
        let found = schema::dep_kind(c, dep_id)?;
        self.dep.insert(dep_id, found.clone());
        Ok(found)
    }

    fn statement(
        &mut self,
        c: &Connection,
        stmt_id: i64,
    ) -> Result<Option<schema::StatementRow>, String> {
        if let Some(known) = self.stmt.get(&stmt_id) {
            return Ok(known.clone());
        }
        let found = schema::statement(c, stmt_id)?;
        self.stmt.insert(stmt_id, found.clone());
        Ok(found)
    }

    fn body_local(&mut self, c: &Connection, net: i64) -> Result<bool, String> {
        if let Some(known) = self.body_local.get(&net) {
            return Ok(*known);
        }
        let found = schema::is_body_local(c, net)?;
        self.body_local.insert(net, found);
        Ok(found)
    }

    /// Whether a procedure runs on an edge.
    fn proc_is_clocked(&mut self, c: &Connection, proc_id: i64) -> Result<bool, String> {
        if let Some(known) = self.clocked.get(&proc_id) {
            return Ok(*known);
        }
        let found = match schema::proc_kind(c, proc_id)?.as_deref() {
            Some("always_ff") => true,
            // A bare `always` is clocked when its sensitivity names an edge.
            Some("always") => {
                schema::events_of_procedure(c, proc_id)?.iter().any(|e| e.edge_kind.is_some())
            }
            _ => false,
        };
        self.clocked.insert(proc_id, found);
        Ok(found)
    }

    /// The net's design path, its width, and the range its bits are spelled
    /// against.
    fn path(&mut self, c: &Connection, anchor: &Anchor, net: i64) -> Result<Spelling, String> {
        if let Some(known) = self.path.get(&net) {
            return Ok(known.clone());
        }
        let found = match schema::net_of(c, net)? {
            Some(row) => {
                let decl =
                    row.data_type.as_deref().and_then(|t| bits::declared_range(t, row.width));
                let scope = match self.scope.get(&row.inst_id) {
                    Some(known) => known.clone(),
                    None => {
                        let walked = crate::trace::instance_path(c, anchor, row.inst_id, '.')?;
                        self.scope.insert(row.inst_id, walked.clone());
                        walked
                    }
                };
                (format!("{scope}.{}", row.net_name), row.width, decl)
            }
            None => (format!("<net {net}>"), None, None),
        };
        self.path.insert(net, found.clone());
        Ok(found)
    }
}

/// Walk outward from `start`, in `dir`, within `bounds`.
pub fn walk(
    db: &Db,
    anchor: &Anchor,
    start: &ResolvedSignal,
    dir: Direction,
    window: Option<(u64, u64)>,
    bounds: Bounds,
) -> Result<Cone, WalkError> {
    walk_to(db, anchor, start, dir, window, bounds, None)
}

/// The same walk, stopping at the level that reaches `goal`.
///
/// Breadth-first order is what makes that safe: every node of a shortest route
/// to the goal arrived at a strictly smaller depth, so the level the goal turns
/// up in is the last one a route can need. A cone whose whole point is one
/// route otherwise costs a design-wide walk to answer about a net one hop away.
pub fn walk_to(
    db: &Db,
    anchor: &Anchor,
    start: &ResolvedSignal,
    dir: Direction,
    window: Option<(u64, u64)>,
    bounds: Bounds,
    goal: Option<i64>,
) -> Result<Cone, WalkError> {
    let c = db.conn();
    let mut facts = Facts::default();

    let mut cover: HashMap<(i64, Option<i64>), Cover> = HashMap::new();
    let mut emitted: HashSet<(i64, i64, Option<i64>, BitSpan, BitSpan)> = HashSet::new();
    let mut edges: Vec<Edge> = Vec::new();
    let mut depth_of: HashMap<i64, u32> = HashMap::new();
    let mut widened = 0usize;
    let mut unresolved: HashSet<i64> = HashSet::new();
    // The deepest level the walk finished, and its size. Kept so a walk that
    // gives up can name a depth the caller can ask for instead.
    let mut complete: (u32, usize, usize) = (0, 1, 0);

    let mut frontier =
        vec![Site { net: start.net.net_id, ctx: None, window: window.unwrap_or(WHOLE), depth: 0 }];
    depth_of.insert(start.net.net_id, 0);

    // One level at a time, and one query per level rather than one per site:
    // the arcs of a whole breadth-first level are asked for together, which on
    // a design-wide cone is three times less work than asking net by net.
    while !frontier.is_empty() {
        let mut asking: Vec<Site> = Vec::new();
        for site in frontier.drain(..) {
            let seen = cover.entry((site.net, site.ctx)).or_default();
            if seen.holds(site.window) {
                continue;
            }
            seen.add(site.window);
            asking.push(site);
        }
        if asking.is_empty() {
            break;
        }

        let mut nets: Vec<i64> = asking.iter().map(|s| s.net).collect();
        nets.sort_unstable();
        nets.dedup();
        let mut by_net: HashMap<i64, Vec<schema::ArcRow>> = HashMap::new();
        for mut row in schema::arcs_of(c, &nets, dir)? {
            if let Some(dep) = row.dep_id
                && let Some((kind, prim)) = facts.dep_kind(c, dep)?
            {
                row.prim_id = row.prim_id.or(prim);
                row.dep_kind = Some(kind);
            }
            by_net.entry(row.signal_net_id).or_default().push(row);
        }

        let mut next: Vec<Site> = Vec::new();
        for site in &asking {
            let Some(rows) = by_net.get(&site.net) else { continue };
            for row in rows {
                let is_control = row.dep_kind.as_deref() == Some("control");
                if is_control && bounds.gating == Gating::None {
                    continue;
                }
                if site.window != WHOLE && !row.signal_bits.may_touch(site.window.0, site.window.1)
                {
                    continue;
                }
                if !admissible(facts.call_parent(c)?, row.call_site_id, site.ctx) {
                    continue;
                }
                let Some(far) = row.other_net_id else {
                    // Nothing to continue to: a tie-off, a boundary, a reader
                    // with no nameable target. `trace` is where those are
                    // reported one by one. A far end the export named but did
                    // not resolve is the one that leaves the answer short, so
                    // that much is counted here: without it a cone missing a
                    // hierarchical driver reads exactly like a complete one.
                    if row.other_ref.is_some() {
                        unresolved.insert(site.net);
                    }
                    continue;
                };

                let (clocked, latch) = facts.state_element(c, far)?;
                // In a combinational walk the far node is where the value stops
                // being this cycle's. The arc to it is still reported — a cone
                // that fell silent could not be told from one that found
                // nothing — but the walk does not go on past it.
                let at_state = bounds.comb && (clocked || (latch && !bounds.through_latch));

                let stmt = match row.stmt_id {
                    Some(id) => facts.statement(c, id)?,
                    None => None,
                };
                let (src, tgt) = match dir {
                    Direction::Driver => (far, site.net),
                    Direction::Load => (site.net, far),
                };
                let (src_bits, tgt_bits) = match dir {
                    Direction::Driver => (row.other_bits, row.signal_bits),
                    Direction::Load => (row.signal_bits, row.other_bits),
                };
                let kind = crate::trace::classify(row, stmt.as_ref());
                let edge = Edge {
                    source: src,
                    target: tgt,
                    kind,
                    raw_kind: crate::trace::raw_kind_of(row, stmt.as_ref(), kind),
                    boundary: row.kind == "connection"
                        || row.kind == "connection_expression"
                        || row.other_ref.is_some(),
                    control: is_control,
                    clocked: match stmt.as_ref().and_then(|s| s.proc_id) {
                        Some(proc_id) => facts.proc_is_clocked(c, proc_id)?,
                        None => false,
                    },
                    src_bits,
                    tgt_bits,
                    map_exact: row.map_exact,
                    ends_at_state: at_state,
                    unreachable: facts.unreachable(c, stmt.as_ref())?,
                    stmt_id: row.stmt_id,
                    call_site_id: row.call_site_id,
                    file: row.file_path.as_deref().map(|p| facts.file(p)),
                    line: row.src_line,
                    depth: site.depth + 1,
                };
                // Whether this arc is worth reporting again and whether the
                // walk continues past it are separate: a row reached with a
                // second window is one edge and two questions, and folding
                // them dropped the second question's answer.
                let next_depth = edge.depth;
                if emitted.insert(edge.key()) {
                    edges.push(edge);
                }
                // The far net belongs to the answer even where the walk stops
                // there: an edge whose endpoint is not among the nodes would
                // leave a caller reading `nodes` as the things `edges` names
                // with a name that is not in it.
                if depth_of.len() >= bounds.max_nodes.unwrap_or(usize::MAX)
                    && !depth_of.contains_key(&far)
                {
                    return Err(WalkError::TooLarge {
                        max_nodes: bounds.max_nodes.unwrap_or(usize::MAX),
                        nodes: depth_of.len(),
                        depth: complete.0,
                        depth_nodes: complete.1,
                        depth_edges: complete.2,
                    });
                }
                depth_of.entry(far).or_insert(next_depth);
                // The gate is named and the walk stops there: where it came
                // from is the next question, asked about it. Following it here
                // is what turned every cone into the same one.
                if is_control && bounds.gating != Gating::Full {
                    continue;
                }
                if at_state {
                    continue;
                }
                if bounds.max_depth.is_some_and(|max| next_depth >= max) {
                    continue;
                }

                let next_window = match site.window {
                    WHOLE => WHOLE,
                    w => match bits::cross(Some(w), row.signal_bits, row.other_bits, row.map_exact)
                    {
                        Some(carried) => carried,
                        None => {
                            // The correspondence broke, so the question widens
                            // to the whole far object rather than naming bits
                            // that might not be the ones.
                            widened += 1;
                            WHOLE
                        }
                    },
                };
                next.push(Site {
                    net: far,
                    ctx: next_ctx(c, row, site.ctx, far, &mut facts)?,
                    window: next_window,
                    depth: next_depth,
                });
            }
        }
        complete = (
            asking.first().map(|s| s.depth).unwrap_or(0) + 1,
            depth_of.len(),
            edges.len(),
        );
        if goal.is_some_and(|net| depth_of.contains_key(&net)) {
            break;
        }
        frontier = next;
    }

    let mut nodes = Vec::with_capacity(depth_of.len());
    for (net, depth) in &depth_of {
        let (path, width, decl) = facts.path(c, anchor, *net)?;
        let (clocked, latch) = facts.state_element(c, *net)?;
        nodes.push(Node { net: *net, path, depth: *depth, width, decl, clocked, latch });
    }
    nodes.sort_by(|a, b| (a.depth, &a.path).cmp(&(b.depth, &b.path)));
    edges.sort_by_key(|e| (e.depth, e.line, e.source, e.target));

    Ok(Cone {
        start: start.path(&anchor.root_name, '.'),
        direction: dir,
        bounds,
        nodes,
        edges,
        widened,
        unresolved: {
            let mut nets: Vec<i64> = unresolved.into_iter().collect();
            nets.sort_unstable();
            nets
        },
    })
}

/// Whether a row's call context may be entered from the site's.
///
/// A body is walked once per call and its formals are shared between the
/// calls, so following every row that touches one would build a path out of
/// one call's condition and another's argument — a combination no execution
/// makes. Module-level rows always pass; a call may be entered from outside
/// it, and left the way it was entered.
fn admissible(parent: &HashMap<i64, Option<i64>>, row_site: Option<i64>, ctx: Option<i64>) -> bool {
    match (row_site, ctx) {
        // A module-level row belongs to no call and passes always; a walk not
        // inside a call may enter any.
        (None, _) | (_, None) => true,
        // The same expansion, or the one nested directly inside it. A sibling
        // expansion is what this refuses: following it would build a path out
        // of one call's condition and another's argument.
        (Some(row), Some(here)) => row == here || parent.get(&row) == Some(&Some(here)),
    }
}

/// The call context on the far side of an arc.
///
/// A binding row is the body's boundary and crossing it always changes the
/// level: entering, the walk takes the call; leaving, it returns to whatever
/// enclosed that call. Any other row stays where it is, and a net the module
/// also names is where a call is left behind in any case.
fn next_ctx(
    c: &Connection,
    row: &schema::ArcRow,
    ctx: Option<i64>,
    far: i64,
    facts: &mut Facts,
) -> Result<Option<i64>, String> {
    let Some(site) = row.call_site_id else {
        return Ok(if facts.body_local(c, far)? { ctx } else { None });
    };
    if row.dep_kind.as_deref() == Some("procedure") {
        let leaving = ctx == Some(site);
        let outer = *facts.call_parent(c)?.get(&site).unwrap_or(&None);
        let next = if leaving { outer } else { Some(site) };
        return Ok(if facts.body_local(c, far)? { next } else { None });
    }
    Ok(if facts.body_local(c, far)? { Some(site) } else { None })
}

/// A directed path from `start` to `goal`, or none where there is no route.
///
/// Breadth-first, so the path found is a shortest one. Not finding one is an
/// ordinary answer: two nets with no route between them is a fact about the
/// design.
pub fn find_path(
    db: &Db,
    anchor: &Anchor,
    start: &ResolvedSignal,
    goal: &ResolvedSignal,
    bounds: Bounds,
) -> Result<Option<Vec<Edge>>, WalkError> {
    if start.net.net_id == goal.net.net_id {
        return Ok(Some(Vec::new()));
    }
    // The walk is the same one; what differs is that it stops at the level the
    // goal turns up in, and is then read backwards from it.
    let cone = walk_to(db, anchor, start, Direction::Load, None, bounds, Some(goal.net.net_id))?;

    // The shallowest arriving edge per node, which breadth-first order makes
    // one on a shortest route. Taken by depth rather than by position: the
    // edges are sorted for presentation, and a route's correctness must not
    // rest on what that sort happens to put first.
    let mut arrival: HashMap<i64, &Edge> = HashMap::new();
    for edge in &cone.edges {
        arrival
            .entry(edge.target)
            .and_modify(|best| {
                if edge.depth < best.depth {
                    *best = edge;
                }
            })
            .or_insert(edge);
    }
    if !arrival.contains_key(&goal.net.net_id) {
        return Ok(None);
    }

    let mut route = Vec::new();
    let mut at = goal.net.net_id;
    while at != start.net.net_id {
        let Some(edge) = arrival.get(&at) else { return Ok(None) };
        route.push((*edge).clone());
        at = edge.source;
    }
    route.reverse();
    Ok(Some(route))
}
