//! `trace` — what drives a signal, what reads it, and which statement did it.
//!
//! One hop. Each arc the database records becomes one reported hop, grouped by
//! the thing that produced it: a statement, a gate, a port crossing. Following
//! a hop is the caller's next call, and following it automatically is what the
//! cone commands are for.
//!
//! A hop carries the statement's own words — its construct, its assignment
//! kind, the operands it reads, the conditions that gate it, the events its
//! procedure triggers on — because "who drives this" is rarely the whole
//! question. What it does not carry is a verdict: which of several drivers was
//! in effect at some moment is not a structural fact, and the material to
//! decide it is here instead.

use std::collections::BTreeSet;

use designdb::bits::{self, BitSpan};
use designdb::resolve::{Anchor, ResolvedSignal};
use designdb::source::{SourceCache, SourceState};
use designdb::{Connection, Db, Direction, schema};
use serde_json::{Value, json};

use crate::envelope::CommandResult;

/// What produced a hop, folded from the database's own kinds into the words a
/// caller reasons about. The database's word travels alongside in `raw_kind`,
/// so a producer that widens its vocabulary is reported rather than lost.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HopKind {
    /// A continuous assignment.
    ContAssign,
    /// An assignment inside a procedure.
    Procedural,
    /// A port crossing the value passes through.
    Port,
    /// A gate, switch or UDP.
    Gate,
    /// A tie-off or a constant right-hand side.
    Constant,
    /// An `alias` binds the two names into one object.
    Alias,
    /// A reference this export has no net for: an upward name, a `$unit` item.
    External,
    /// A system task wrote it, from a file or a plusarg.
    SystemTask,
    /// A `-> ev` fired the event.
    Trigger,
    /// The design boundary: a root port, where the world outside drives.
    Terminal,
    /// A procedure triggers on it — a flop's clock pin is a load of its clock.
    Sensitivity,
    /// A statement waits on it.
    Wait,
    /// A condition gates the target rather than supplying its value.
    Control,
    /// A call binds it to a subroutine's formal.
    Call,
    /// A statement reads it but writes nothing this instance names: an
    /// assertion, a `$display`.
    Statement,
    /// A statement that neither assigns nor is any of the above.
    Other,
}

impl HopKind {
    pub fn tag(self) -> &'static str {
        match self {
            HopKind::ContAssign => "continuous_assign",
            HopKind::Procedural => "procedural",
            HopKind::Port => "port",
            HopKind::Gate => "gate",
            HopKind::Constant => "constant",
            HopKind::Alias => "alias",
            HopKind::External => "external",
            HopKind::SystemTask => "system_task",
            HopKind::Trigger => "trigger",
            HopKind::Terminal => "terminal",
            HopKind::Sensitivity => "sensitivity",
            HopKind::Wait => "wait",
            HopKind::Control => "control",
            HopKind::Call => "call",
            HopKind::Statement => "statement",
            HopKind::Other => "other",
        }
    }

    /// Whether a hop of this kind is a statement about where the value goes or
    /// comes from, rather than about how much detail was asked for.
    ///
    /// An alias binds two names into one object without either driving the
    /// other, so an aliased net with no driver would otherwise read as
    /// resolved. A condition is excluded for a different reason: `--control`
    /// changes how much is displayed and must not change what the tool says
    /// about the design.
    ///
    /// A sensitivity is NOT excluded. Membership follows the netlist model: a
    /// flop's clock pin is a load of its clock, so a clock whose only answers
    /// are sensitivities has loads.
    fn is_structural(self) -> bool {
        !matches!(self, HopKind::Alias | HopKind::Control)
    }
}

/// One gating level over a statement.
#[derive(Debug, Clone)]
struct Gate {
    kind: String,
    /// `then`/`else` on an `if`, so the two arms are told apart.
    sense: Option<String>,
    case_kind: Option<String>,
    check_kind: Option<String>,
    /// Written position among siblings — a plain case's priority. Absent on
    /// the levels that have no siblings to be ordered among.
    ordinal: Option<i64>,
    /// The compile-time verdict, where the condition is constant.
    static_taken: Option<i64>,
    labels: Option<String>,
    /// The nets read in this level's condition.
    reads: Vec<String>,
    /// A loop's iteration space, which says what an inexact range covers.
    iteration: Option<String>,
    line: Option<u32>,
}

/// One recorded arc, with everything the statement behind it says.
#[derive(Debug, Clone)]
pub struct Hop {
    pub kind: HopKind,
    /// The database's own word for this arc or construct.
    pub raw_kind: String,
    /// The statement's text, where the source still matches the export.
    statement: Option<String>,
    source_state: SourceState,
    /// The instance the statement lives in.
    scope: String,
    file: Option<String>,
    line: Option<u32>,
    /// Whether the value crossed a hierarchy boundary to get here.
    boundary: bool,
    /// Which bits of the traced signal this hop's arcs touch. More than one
    /// where a statement writes several windows: `{y[7:6], y[3:2]} = ...` is
    /// two, and folding them into one range would claim the bits between.
    spans: Vec<BitSpan>,
    /// False where a range is an upper bound rather than the bits touched.
    bits_exact: bool,
    /// The full paths of the nets at the other end.
    signals: Vec<String>,
    /// `continuous | blocking | nonblocking`.
    assign_kind: Option<String>,
    /// Execution order inside the procedure, where there is one.
    sequence: Option<i64>,
    /// The procedure's kind and the events it triggers on.
    timing: Option<Timing>,
    /// The conditions the statement sits under, outermost first.
    gates: Vec<Gate>,
    /// Whether a level of the gating cannot be reached at this parameterisation.
    unreachable: bool,
    /// The call string, where the statement came from a subroutine body.
    call_chain: Vec<String>,
    /// Every assignment this procedure makes to this signal, in the order they
    /// run. Which one is in effect is a matter of that order and of what gates
    /// each — a `y = '0` before a case is a default the arms overwrite — and
    /// deciding it is the reader's.
    procedure_writes: Vec<Write>,
    /// What identifies the arc, so two arcs of one statement are one hop.
    provenance: String,
    /// What identifies the DRIVING SOURCE, which is coarser: the two arms of
    /// one `if` are two statements and one driver, since a procedure drives as
    /// a whole and its statements cannot contend with each other.
    source_key: String,
}

/// One of a procedure's assignments to the signal being traced.
#[derive(Debug, Clone)]
struct Write {
    /// Execution order within the procedure. Later overwrites earlier.
    sequence: Option<i64>,
    line: Option<u32>,
    /// The statement's text, where the source still matches the export.
    statement: Option<String>,
    /// Whether this is the assignment the hop is about.
    is_this: bool,
    /// Whether nothing gates it — an unconditional write, which every earlier
    /// one is overwritten by and which is what a default looks like.
    unconditional: bool,
}

/// What makes a procedure run.
#[derive(Debug, Clone)]
struct Timing {
    /// `always_ff | always_comb | always_latch | always | initial | final`.
    proc_kind: String,
    /// One entry per event of an explicit sensitivity list. The clock is not
    /// identified: both events of `@(posedge clk or negedge rst_n)` are here.
    events: Vec<(Option<String>, String)>,
}

pub struct Trace {
    signal: String,
    direction: Direction,
    /// The bit range asked about, spelled in declared indices.
    bits: Option<String>,
    width: Option<i64>,
    /// The signal's own declared range, which is what a span is spelled
    /// against. `None` for a type that has no single one — an aggregate.
    decl: Option<(i64, i64)>,
    hops: Vec<Hop>,
    /// Distinct sources driving the signal, by the thing that produced them.
    driver_count: usize,
    /// Whether two drivers' ranges overlap, which is a conflict rather than a
    /// signal assembled from parts.
    conflicting: bool,
}

impl Trace {
    fn status(&self) -> &'static str {
        let structural: Vec<&Hop> = self.hops.iter().filter(|h| h.kind.is_structural()).collect();
        if structural.is_empty() {
            match self.direction {
                Direction::Driver => "no_driver_found",
                Direction::Load => "no_load_found",
            }
        } else if structural.iter().all(|h| matches!(h.kind, HopKind::Port | HopKind::Terminal)) {
            // Every answer was a boundary: what is on the other side was not
            // asked for, and is a different call rather than nothing.
            "boundary_only"
        } else {
            "resolved"
        }
    }
}

fn gate_json(g: &Gate) -> Value {
    json!({
        "kind": g.kind,
        "sense": g.sense,
        "case_kind": g.case_kind,
        "check": g.check_kind,
        "ordinal": g.ordinal,
        "labels": g.labels,
        "reads": g.reads,
        "static_taken": g.static_taken,
        "iteration": g.iteration,
        "line": g.line,
    })
}

impl CommandResult for Trace {
    fn to_json(&self) -> (Value, Value) {
        let hops: Vec<Value> = self
            .hops
            .iter()
            .map(|h| {
                json!({
                    "kind": h.kind.tag(),
                    "raw_kind": h.raw_kind,
                    "statement": h.statement,
                    "source": h.source_state.tag(),
                    "scope": h.scope,
                    "file": h.file,
                    "line": h.line,
                    "boundary": h.boundary,
                    "bits": spell_spans(&h.spans, self.decl),
                    "bits_exact": h.bits_exact,
                    "signals": h.signals,
                    "assign_kind": h.assign_kind,
                    "sequence": h.sequence,
                    "timing": h.timing.as_ref().map(|t| json!({
                        "proc_kind": t.proc_kind,
                        "events": t.events.iter().map(|(edge, net)| json!({
                            "edge": edge, "signal": net,
                        })).collect::<Vec<_>>(),
                    })),
                    "gates": h.gates.iter().map(gate_json).collect::<Vec<_>>(),
                    "procedure_writes": h.procedure_writes.iter().map(|w| json!({
                        "sequence": w.sequence,
                        "line": w.line,
                        "statement": w.statement,
                        "is_this": w.is_this,
                        "unconditional": w.unconditional,
                    })).collect::<Vec<_>>(),
                    "unreachable": h.unreachable,
                    "call_chain": h.call_chain,
                })
            })
            .collect();

        let data = json!({
            "signal": self.signal,
            "direction": self.direction.tag(),
            "bits": self.bits,
            "width": self.width,
            "status": self.status(),
            "hops": hops,
        });
        let summary = json!({
            "status": self.status(),
            "hops": self.hops.len(),
            "drivers": self.driver_count,
            "multiple_drivers": self.conflicting,
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let mut out = String::new();
        let width = self.width.map(|w| format!("[{w} bits]")).unwrap_or_default();
        out.push_str(&format!(
            "Signal: {}{} {width}\n",
            self.signal,
            self.bits.as_deref().unwrap_or("")
        ));
        // Hops and sources are different counts and the difference matters: an
        // `if`/`else` is two statements and one driver.
        let count = match self.direction {
            Direction::Driver if self.driver_count != self.hops.len() => {
                format!("{} statement(s) from {} source(s)", self.hops.len(), self.driver_count)
            }
            _ => format!("{}", self.hops.len()),
        };
        out.push_str(&format!("{}s: {count} ({})\n", self.direction.tag(), self.status()));
        if self.conflicting {
            out.push_str("  ! drivers overlap: more than one writes the same bits\n");
        }
        out.push('\n');

        for hop in &self.hops {
            let text = hop
                .statement
                .clone()
                .unwrap_or_else(|| format!("<{}>", hop.raw_kind));
            let at = match (&hop.file, hop.line) {
                (Some(f), Some(l)) => {
                    format!("{}:{l}", f.rsplit('/').next().unwrap_or(f))
                }
                _ => String::new(),
            };
            out.push_str(&format!("  {:<18} {:<22} {text}\n", hop.kind.tag(), at));

            if let Some(b) = spell_spans(&hop.spans, self.decl) {
                let note = if hop.bits_exact { "" } else { " (upper bound)" };
                out.push_str(&format!("      bits: {}{note}\n", render_bits(&b)));
            }
            // Why the text is a placeholder is part of the answer: a file that
            // has moved on names lines that are no longer these.
            if hop.source_state != SourceState::Read {
                out.push_str(&format!("      source: {}\n", hop.source_state.tag()));
            }
            if let Some(t) = &hop.timing {
                let events: Vec<String> = t
                    .events
                    .iter()
                    .map(|(edge, net)| match edge {
                        Some(e) => format!("{e} {net}"),
                        None => net.clone(),
                    })
                    .collect();
                let list =
                    if events.is_empty() { String::new() } else { format!(" @({})", events.join(", ")) };
                out.push_str(&format!("      timing: {}{list}\n", t.proc_kind));
            }
            for g in &hop.gates {
                out.push_str(&format!("      when: {}\n", describe_gate(g)));
            }
            if hop.unreachable {
                out.push_str("      unreachable: a constant condition rules this arm out\n");
            }
            if !hop.call_chain.is_empty() {
                out.push_str(&format!("      via: {}\n", hop.call_chain.join(" -> ")));
            }
            // Where a procedure writes the signal more than once, the order is
            // the answer to "which one held": later overwrites earlier, and an
            // unconditional write overwrites everything before it.
            if hop.procedure_writes.len() > 1 {
                out.push_str(&format!(
                    "      in this {} ({} writes, in order):\n",
                    hop.timing.as_ref().map(|t| t.proc_kind.as_str()).unwrap_or("procedure"),
                    hop.procedure_writes.len()
                ));
                for w in &hop.procedure_writes {
                    out.push_str(&format!(
                        "        {} {:>4}  {}{}\n",
                        if w.is_this { "->" } else { "  " },
                        w.line.map(|l| l.to_string()).unwrap_or_default(),
                        w.statement.as_deref().unwrap_or("<assignment>"),
                        if w.unconditional { "   [unconditional]" } else { "" },
                    ));
                }
            }
            for s in &hop.signals {
                out.push_str(&format!("      {}: {s}\n", match self.direction {
                    Direction::Driver => "from",
                    Direction::Load => "to",
                }));
            }
        }
        out
    }
}

/// One gating level in a sentence, for the terminal view.
fn describe_gate(g: &Gate) -> String {
    let reads = if g.reads.is_empty() { String::new() } else { format!(" [{}]", g.reads.join(", ")) };
    let body = match g.kind.as_str() {
        "if" => format!("if{}", g.sense.as_deref().map(|s| format!(" ({s})")).unwrap_or_default()),
        "case" => format!("{} selector", g.case_kind.as_deref().unwrap_or("case")),
        "case_item" => format!(
            "case item{}{}",
            g.ordinal.map(|o| format!(" #{o}")).unwrap_or_default(),
            g.labels.as_deref().map(|l| format!(" ({l})")).unwrap_or_default()
        ),
        "case_default" => "case default".to_string(),
        "loop" => match &g.iteration {
            Some(space) => format!("loop {space}"),
            None => "loop".to_string(),
        },
        other => other.to_string(),
    };
    let verdict = match g.static_taken {
        Some(0) => "  <- never taken",
        Some(_) => "  <- always taken",
        None => "",
    };
    format!("{body}{reads}{verdict}")
}

/// Trace one signal, one hop.
pub fn run(
    db: &Db,
    anchor: &Anchor,
    signal: &ResolvedSignal,
    dir: Direction,
    select: Option<(u64, u64)>,
    spelled_bits: Option<String>,
    control: bool,
) -> Result<Trace, String> {
    let c = db.conn();
    let mut source = SourceCache::new();
    let sep = '.';

    let mut rows = schema::arcs(c, signal.net.net_id, dir)?;
    for row in &mut rows {
        // Every dependency row, not only the ones `v_load` calls `dataflow`:
        // `v_driver` reports a control dependency whose source is a
        // hierarchical name as `external`, which says where the source is but
        // not that it gates rather than drives. The kind is a per-row lookup
        // and not a join, since joining the two views makes SQLite
        // materialise both.
        if let Some(dep) = row.dep_id
            && let Some((kind, prim)) = schema::dep_kind(c, dep)?
        {
            row.prim_id = row.prim_id.or(prim);
            row.dep_kind = Some(kind);
        }
    }

    let mut hops: Vec<Hop> = Vec::new();
    for (index, row) in rows.into_iter().enumerate() {
        if let Some((lo, hi)) = select
            && !row.signal_bits.may_touch(lo, hi)
        {
            continue;
        }
        // A condition reaches its target through a branch, not through a
        // value. The levels it came from are on the hop either way (`gates`),
        // so listing the condition nets among the reads is asked for.
        if !control && row.dep_kind.as_deref() == Some("control") {
            continue;
        }
        let provenance = provenance_of(&row, index);
        match hops.iter_mut().find(|h| h.provenance == provenance) {
            // One statement, one hop: `{a,b} = {x,y}` is two arcs of one
            // statement, and reporting it twice would count one driver twice.
            Some(existing) => merge(existing, &row, anchor, signal, sep, c)?,
            None => {
                hops.push(build_hop(c, &mut source, anchor, signal, &row, index, sep, dir)?)
            }
        }
    }

    hops.sort_by(|a, b| {
        (&a.scope, a.line, &a.statement).cmp(&(&b.scope, b.line, &b.statement))
    });

    let (driver_count, conflicting) = count_drivers(&hops, dir);
    Ok(Trace {
        signal: signal.path(&anchor.root_name, sep),
        direction: dir,
        bits: spelled_bits,
        width: signal.net.width,
        decl: declared_range_of(signal),
        hops,
        driver_count,
        conflicting,
    })
}

/// What identifies one answer, so two arcs of one statement are one hop and
/// two statements are two.
///
/// The role is part of it: a condition and a value are different facts about
/// the same statement, and folding them together would present a gating net as
/// something the statement reads for its value.
///
/// `index` distinguishes rows that name nothing else. A sensitivity row belongs
/// to a procedure's header and carries no statement, so without it every flop
/// clocked by one net would collapse into a single answer.
fn declared_range_of(signal: &ResolvedSignal) -> Option<(i64, i64)> {
    bits::declared_range(signal.net.data_type.as_deref()?, signal.net.width)
}

fn provenance_of(row: &schema::ArcRow, index: usize) -> String {
    let role = match row.dep_kind.as_deref() {
        Some("control") => ":control",
        _ => "",
    };
    match (row.stmt_id, row.conn_id, row.prim_id, row.term_id, row.proc_id) {
        (Some(s), ..) => format!("s{s}{role}"),
        (_, Some(c), ..) => format!("c{c}"),
        (_, _, Some(p), ..) => format!("p{p}"),
        (_, _, _, Some(t), _) => format!("t{t}"),
        (.., Some(p)) => format!("proc{p}"),
        _ => format!("row{index}"),
    }
}

/// How many distinct sources drive the signal, and whether any two contend.
///
/// The unit is the source, not the row. A procedure drives as a whole — its
/// own statements run in order and cannot contend with each other — so an
/// `if`/`else` writing one variable is one driver reported as two statements.
/// Two procedures, or an assignment and a port, are two.
///
/// Kinds that gate or bind rather than drive are excluded, as is an arm a
/// constant condition already rules out. Ranges decide contention: a signal
/// assembled from disjoint slices has several drivers and no contest.
fn count_drivers(hops: &[Hop], dir: Direction) -> (usize, bool) {
    if dir != Direction::Driver {
        return (0, false);
    }
    let driving: Vec<&Hop> = hops
        .iter()
        .filter(|h| !matches!(h.kind, HopKind::Alias | HopKind::Terminal | HopKind::Sensitivity))
        .filter(|h| !h.unreachable)
        .collect();

    let sources: BTreeSet<&str> = driving.iter().map(|h| h.source_key.as_str()).collect();
    let mut contend = false;
    for (i, a) in driving.iter().enumerate() {
        for b in &driving[i + 1..] {
            if a.source_key != b.source_key
                && a.spans.iter().any(|x| b.spans.iter().any(|y| x.overlaps(y)))
            {
                contend = true;
            }
        }
    }
    (sources.len(), contend)
}

/// The windows a hop touches, in declared indices. `None` where there is
/// nothing to say: the whole object, or no declared range to anchor offsets
/// against.
fn spell_spans(spans: &[BitSpan], decl: Option<(i64, i64)>) -> Option<Vec<String>> {
    let spelled: Vec<String> = spans.iter().filter_map(|s| s.spell(decl)).collect();
    (!spelled.is_empty()).then_some(spelled)
}

/// Several windows read as a list; one reads as itself.
fn render_bits(spelled: &[String]) -> String {
    spelled.join(", ")
}

/// Add another arc of an already-reported statement: the ends differ, the
/// statement does not.
///
/// Every field is folded rather than left at whichever arc arrived first —
/// `arcs()` has no order that is a contract, and a statement that crosses a
/// boundary in one of its arcs crosses one.
fn merge(
    hop: &mut Hop,
    row: &schema::ArcRow,
    anchor: &Anchor,
    signal: &ResolvedSignal,
    sep: char,
    c: &Connection,
) -> Result<(), String> {
    if let Some(path) = far_path(c, anchor, row, sep)?
        && !hop.signals.contains(&path)
    {
        hop.signals.push(path);
    }
    if !hop.spans.contains(&row.signal_bits) {
        hop.spans.push(row.signal_bits);
    }
    hop.bits_exact &= row.signal_bits.is_exact() && row.map_exact != Some(false);
    hop.boundary |= crosses_boundary(row, signal);
    Ok(())
}

/// Whether an arc reached this signal from outside the instance it lives in.
fn crosses_boundary(row: &schema::ArcRow, signal: &ResolvedSignal) -> bool {
    row.kind == "connection"
        || row.kind == "connection_expression"
        || row.other_ref.is_some()
        || row.other_inst_id.is_some_and(|i| i != signal.inst)
}

/// The full path of the net at the far end of an arc.
fn far_path(
    c: &Connection,
    anchor: &Anchor,
    row: &schema::ArcRow,
    sep: char,
) -> Result<Option<String>, String> {
    let (Some(inst), Some(name)) = (row.other_inst_id, row.other_name.as_deref()) else {
        // Not a net this export names: the reference, where there is one, is
        // the only name there is.
        return Ok(row.other_ref.clone());
    };
    Ok(Some(instance_path(c, anchor, inst, sep)? + &sep.to_string() + &name.replace('.', &sep.to_string())))
}

/// The path of one instance, from the root.
pub fn instance_path(
    c: &Connection,
    anchor: &Anchor,
    inst: i64,
    sep: char,
) -> Result<String, String> {
    match designdb::resolve::path_below_root(c, anchor.root, inst)? {
        Some(spine) => {
            let mut parts = vec![anchor.root_name.clone()];
            parts.extend(spine);
            Ok(parts.join(&sep.to_string()))
        }
        // A package sits above the roots and is in no design path: it answers
        // to its own name, and claiming it lives inside the design would be a
        // path that does not exist.
        None => Ok(designdb::schema::node(c, inst)?
            .map(|n| n.node_name)
            .unwrap_or_else(|| format!("<instance {inst}>"))),
    }
}

#[allow(clippy::too_many_arguments)]
fn build_hop(
    c: &Connection,
    source: &mut SourceCache,
    anchor: &Anchor,
    signal: &ResolvedSignal,
    row: &schema::ArcRow,
    index: usize,
    sep: char,
    dir: Direction,
) -> Result<Hop, String> {
    let stmt = match row.stmt_id {
        Some(id) => schema::statement(c, id)?,
        None => None,
    };
    let kind = classify(row, stmt.as_ref());
    let raw_kind = raw_kind_of(row, stmt.as_ref(), kind);

    let scope_inst = stmt.as_ref().map(|s| s.inst_id).or(row.other_inst_id).unwrap_or(signal.inst);
    let scope = instance_path(c, anchor, scope_inst, sep)?;

    let (file, line) = match &stmt {
        Some(s) if s.file_path.is_some() => (s.file_path.clone(), s.src_line),
        _ => (row.file_path.clone(), row.src_line),
    };
    let (statement, source_state) = match (&file, line) {
        (Some(f), Some(l)) => source.line(c, f, l),
        _ => (None, SourceState::Missing),
    };

    let mut hop = Hop {
        kind,
        raw_kind,
        statement,
        source_state,
        scope,
        file,
        line,
        boundary: crosses_boundary(row, signal),
        spans: vec![row.signal_bits],
        bits_exact: row.signal_bits.is_exact() && row.map_exact != Some(false),
        signals: far_path(c, anchor, row, sep)?.into_iter().collect(),
        assign_kind: stmt.as_ref().and_then(|s| s.assign_kind.clone()),
        sequence: stmt.as_ref().and_then(|s| s.sequence),
        timing: None,
        gates: Vec::new(),
        unreachable: false,
        call_chain: Vec::new(),
        procedure_writes: Vec::new(),
        provenance: provenance_of(row, index),
        source_key: match stmt.as_ref().and_then(|s| s.proc_id) {
            Some(proc) => format!("proc{proc}"),
            None => provenance_of(row, index),
        },
    };

    if let Some(stmt) = &stmt {
        // A driver's operands are what it reads; a load's are not this
        // signal's business, and listing them would name the far side twice.
        if dir == Direction::Driver && hop.signals.is_empty() {
            for operand in schema::operands_of(c, stmt.stmt_id)? {
                let path = instance_path(c, anchor, stmt.inst_id, sep)?
                    + &sep.to_string()
                    + &operand.net_name.replace('.', &sep.to_string());
                if !hop.signals.contains(&path) {
                    hop.signals.push(path);
                }
            }
        }
        hop.timing = timing_of(c, stmt)?;
        (hop.gates, hop.unreachable) = gates_of(c, stmt)?;
        hop.procedure_writes = writes_of(c, source, stmt, signal.net.net_id)?;
        hop.call_chain = match stmt.call_site_id {
            Some(id) => schema::call_chain(c, id)?
                .into_iter()
                .map(|s| format!("{}()", s.subroutine_name))
                .collect(),
            None => Vec::new(),
        };
    }
    Ok(hop)
}

/// Fold a row's kind into the word a caller reasons about.
pub fn classify(row: &schema::ArcRow, stmt: Option<&schema::StatementRow>) -> HopKind {
    // A control dependency is one whichever word the view uses: `v_driver`
    // folds one whose source is a hierarchical name into `external`, which says
    // where the source is but not that it gates rather than drives.
    if row.dep_kind.as_deref() == Some("control") {
        return HopKind::Control;
    }
    // Otherwise the view's own word wins over the dependency's. It is the more
    // specific of the two: a data dependency with no source is `constant`, a
    // `system_task` or a `trigger`, and the dependency kind says only `data`.
    match row.kind.as_str() {
        "procedure" => return HopKind::Call,
        "connection" | "connection_expression" => return HopKind::Port,
        "terminal" => return HopKind::Terminal,
        "constant" => return HopKind::Constant,
        "external" => return HopKind::External,
        "system_task" => return HopKind::SystemTask,
        "trigger" => return HopKind::Trigger,
        "alias" => return HopKind::Alias,
        "primitive" => return HopKind::Gate,
        "sensitivity" => return HopKind::Sensitivity,
        // The database keeps these apart and so does this: a procedure
        // triggering on a net and a statement waiting for one are different
        // things happening at different times.
        "wait" => return HopKind::Wait,
        _ => {}
    }
    // Whatever kind the arc has, a statement that assigns nothing is not an
    // assignment: a `$display` reading the signal names a statement, and
    // calling it procedural would name an assignment that does not exist.
    let Some(stmt) = stmt else { return HopKind::Other };
    match stmt.assign_kind.as_deref() {
        Some("continuous") => HopKind::ContAssign,
        Some(_) => HopKind::Procedural,
        None if row.kind == "statement" => HopKind::Statement,
        None => HopKind::Other,
    }
}

/// The database's own word for this arc, kept beside the folded one.
pub fn raw_kind_of(
    row: &schema::ArcRow,
    stmt: Option<&schema::StatementRow>,
    kind: HopKind,
) -> String {
    match kind {
        HopKind::Port | HopKind::Terminal | HopKind::External | HopKind::Sensitivity => {
            row.kind.clone()
        }
        _ => stmt
            .and_then(|s| s.construct.clone())
            .or_else(|| stmt.map(|s| s.stmt_kind.clone()))
            .unwrap_or_else(|| row.kind.clone()),
    }
}

/// What makes the statement's procedure run.
fn timing_of(
    c: &Connection,
    stmt: &schema::StatementRow,
) -> Result<Option<Timing>, String> {
    let Some(proc_id) = stmt.proc_id else { return Ok(None) };
    let Some(proc_kind) = schema::proc_kind(c, proc_id)? else { return Ok(None) };
    let events = schema::events_of_procedure(c, proc_id)?
        .into_iter()
        .map(|e| (e.edge_kind, e.net_name.unwrap_or_else(|| "<expression>".into())))
        .collect();
    Ok(Some(Timing { proc_kind, events }))
}

/// Every assignment the statement's procedure makes to this signal, in order.
///
/// Only where there is more than one: a single write is the statement already
/// reported, and repeating it would say nothing.
fn writes_of(
    c: &Connection,
    source: &mut SourceCache,
    stmt: &schema::StatementRow,
    net: i64,
) -> Result<Vec<Write>, String> {
    let Some(proc_id) = stmt.proc_id else { return Ok(Vec::new()) };
    let siblings = schema::procedure_writes(c, proc_id, net)?;
    if siblings.len() < 2 {
        return Ok(Vec::new());
    }
    let mut writes = Vec::with_capacity(siblings.len());
    for row in siblings {
        let text = match (&row.file_path, row.src_line) {
            (Some(f), Some(l)) => source.line(c, f, l).0,
            _ => None,
        };
        writes.push(Write {
            sequence: row.sequence,
            line: row.src_line,
            statement: text,
            is_this: row.stmt_id == stmt.stmt_id,
            // No gating level at all: nothing stands between the procedure
            // running and this assignment happening.
            unconditional: row.branch_id.is_none(),
        });
    }
    Ok(writes)
}

/// The conditions a statement sits under, outermost first, and whether any of
/// them is already decided against.
fn gates_of(
    c: &Connection,
    stmt: &schema::StatementRow,
) -> Result<(Vec<Gate>, bool), String> {
    let Some(branch_id) = stmt.branch_id else { return Ok((Vec::new(), false)) };
    let chain = schema::branch_chain(c, branch_id)?;
    let reads = schema::gating_reads(c, stmt.stmt_id)?;

    let mut unreachable = false;
    let gates = chain
        .into_iter()
        .map(|b| {
            if b.static_taken == Some(0) {
                unreachable = true;
            }
            // A read belongs to the level whose condition made it, which is
            // what tells one arm's labels from another's.
            let mut here: BTreeSet<String> = BTreeSet::new();
            for r in reads.iter().filter(|r| r.branch_id == Some(b.branch_id)) {
                here.insert(r.net_name.clone());
            }
            Gate {
                kind: b.branch_kind,
                sense: b.sense,
                case_kind: b.case_kind,
                check_kind: b.check_kind,
                ordinal: b.ordinal,
                static_taken: b.static_taken,
                labels: b.labels,
                reads: here.into_iter().collect(),
                iteration: iteration_of(&b.iter_name, b.iter_first, b.iter_step, b.iter_count),
                line: b.src_line,
            }
        })
        .collect();
    Ok((gates, unreachable))
}

/// A loop's iteration space, which is what separates an inexact range from a
/// smear: `j = 0, 1, ... 7` says what to substitute back.
fn iteration_of(
    name: &Option<String>,
    first: Option<i64>,
    step: Option<i64>,
    count: Option<i64>,
) -> Option<String> {
    let name = name.as_ref()?;
    match (first, step, count) {
        (Some(first), Some(step), Some(count)) => {
            Some(format!("{name} = {first}, step {step}, {count} iteration(s)"))
        }
        _ => Some(name.clone()),
    }
}
