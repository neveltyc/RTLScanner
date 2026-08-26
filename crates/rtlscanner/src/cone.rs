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
    /// Follow the conditions that gate a statement as well as the values it
    /// moves. A condition is a real dependency and a numerous one.
    pub control: bool,
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

/// What the walk needs to know about a net, asked once each.
#[derive(Default)]
struct Facts {
    /// Nets a state element writes, on either side of a port. Read once for
    /// the database and on first use: the relation does not depend on the
    /// walk, and a cone asks about nearly every net it reaches — but a walk
    /// that asks about none should not pay to know where they all are.
    state_nets: Option<(HashSet<i64>, HashSet<i64>)>,
    body_local: HashMap<i64, bool>,
    dep: HashMap<i64, Option<(String, Option<i64>)>>,
    /// Statements, by id. A cone crosses many more arcs than a design has
    /// statements — one `always` block answers for hundreds of them.
    stmt: HashMap<i64, Option<schema::StatementRow>>,
    path: HashMap<i64, (String, Option<i64>)>,
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

    fn file(&mut self, path: &str) -> Rc<str> {
        if let Some(known) = self.files.get(path) {
            return known.clone();
        }
        let shared: Rc<str> = Rc::from(path);
        self.files.insert(path.to_string(), shared.clone());
        shared
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

    /// The net's design path, and its width.
    fn path(
        &mut self,
        c: &Connection,
        anchor: &Anchor,
        net: i64,
    ) -> Result<(String, Option<i64>), String> {
        if let Some(known) = self.path.get(&net) {
            return Ok(known.clone());
        }
        let found = match schema::net_of(c, net)? {
            Some(row) => {
                let scope = match self.scope.get(&row.inst_id) {
                    Some(known) => known.clone(),
                    None => {
                        let walked = crate::trace::instance_path(c, anchor, row.inst_id, '.')?;
                        self.scope.insert(row.inst_id, walked.clone());
                        walked
                    }
                };
                (format!("{scope}.{}", row.net_name), row.width)
            }
            None => (format!("<net {net}>"), None),
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
) -> Result<Cone, String> {
    let c = db.conn();
    let mut facts = Facts::default();

    let mut cover: HashMap<(i64, Option<i64>), Cover> = HashMap::new();
    let mut emitted: HashSet<(i64, i64, Option<i64>, BitSpan, BitSpan)> = HashSet::new();
    let mut edges: Vec<Edge> = Vec::new();
    let mut depth_of: HashMap<i64, u32> = HashMap::new();
    let mut widened = 0usize;

    let mut frontier = vec![Site {
        net: start.net.net_id,
        ctx: None,
        window: window.unwrap_or(WHOLE),
        depth: 0,
    }];
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
                if is_control && !bounds.control {
                    continue;
                }
                if site.window != WHOLE
                    && !row.signal_bits.may_touch(site.window.0, site.window.1)
                {
                    continue;
                }
                if !admissible(row.call_site_id, site.ctx) {
                    continue;
                }
                let Some(far) = row.other_net_id else {
                    // Nothing to continue to: a tie-off, a boundary, a reader
                    // with no nameable target. `trace` is where those are
                    // reported; here they end the walk without ending the
                    // answer.
                    continue;
                };

                let (clocked, latch) = facts.state_element(c, far)?;
                // In a combinational walk the far node is where the value
                // stops being this cycle's: the arc into it goes with it, or
                // the answer would name a boundary it does not include.
                if bounds.comb && (clocked || (latch && !bounds.through_latch)) {
                    continue;
                }

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
                    stmt_id: row.stmt_id,
                    call_site_id: row.call_site_id,
                    file: row.file_path.as_deref().map(|p| facts.file(p)),
                    line: row.src_line,
                    depth: site.depth + 1,
                };
                if !emitted.insert(edge.key()) {
                    continue;
                }
                edges.push(edge);

                let next_depth = site.depth + 1;
                depth_of.entry(far).or_insert(next_depth);
                if bounds.max_depth.is_some_and(|max| next_depth >= max) {
                    continue;
                }

                let next_window = match site.window {
                    WHOLE => WHOLE,
                    w => match bits::cross(
                        Some(w),
                        row.signal_bits,
                        row.other_bits,
                        row.map_exact,
                    ) {
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
        frontier = next;
    }

    let mut nodes = Vec::with_capacity(depth_of.len());
    for (net, depth) in &depth_of {
        let (path, width) = facts.path(c, anchor, *net)?;
        let (clocked, latch) = facts.state_element(c, *net)?;
        nodes.push(Node { net: *net, path, depth: *depth, width, clocked, latch });
    }
    nodes.sort_by(|a, b| (a.depth, &a.path).cmp(&(b.depth, &b.path)));
    edges.sort_by(|a, b| (a.depth, a.line, a.source, a.target).cmp(&(b.depth, b.line, b.source, b.target)));

    Ok(Cone {
        start: start.path(&anchor.root_name, '.'),
        direction: dir,
        bounds,
        nodes,
        edges,
        widened,
    })
}

/// Whether a row's call context may be entered from the site's.
///
/// A body is walked once per call and its formals are shared between the
/// calls, so following every row that touches one would build a path out of
/// one call's condition and another's argument — a combination no execution
/// makes. Module-level rows always pass; a call may be entered from outside
/// it, and left the way it was entered.
fn admissible(row_site: Option<i64>, ctx: Option<i64>) -> bool {
    match (row_site, ctx) {
        (None, _) | (_, None) => true,
        (Some(row), Some(here)) => row == here,
    }
}

/// The call context on the far side of an arc.
///
/// A net that exists only inside a call keeps the call; one the module also
/// names is where the call is left behind.
fn next_ctx(
    c: &Connection,
    row: &schema::ArcRow,
    ctx: Option<i64>,
    far: i64,
    facts: &mut Facts,
) -> Result<Option<i64>, String> {
    let site = row.call_site_id.or(ctx);
    match site {
        Some(site) if facts.body_local(c, far)? => Ok(Some(site)),
        _ => Ok(None),
    }
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
) -> Result<Option<Vec<Edge>>, String> {
    // The walk is the same one; what differs is that it is read backwards from
    // the goal once the goal is in it.
    let cone = walk(db, anchor, start, Direction::Load, None, bounds)?;
    if start.net.net_id == goal.net.net_id {
        return Ok(Some(Vec::new()));
    }

    // One arriving edge per node, the first found — which breadth-first order
    // makes one on a shortest route.
    let mut arrival: HashMap<i64, &Edge> = HashMap::new();
    for edge in &cone.edges {
        arrival.entry(edge.target).or_insert(edge);
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

