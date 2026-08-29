//! The contract surface: rows of the `v_*` views, and the point queries that
//! fetch them.
//!
//! Columns are listed explicitly and read by index into that list, never by
//! position in the view — a schema change then fails to prepare rather than
//! transposing two same-typed neighbours silently.
//!
//! Two rules from the kit hold everywhere here. Every query is a point query
//! that seeks; the closure over them is the engine's, not SQL's. And no two
//! views are joined: SQLite materialises both when they are, which measured
//! three orders of magnitude on the arc queries, so a kind that needs a second
//! row looks it up per row instead.

use std::collections::HashSet;

use rusqlite::Connection;

use crate::bits::BitSpan;
use crate::err;

/// The export's own seal: what produced this database, what it covers, and
/// where the analysis fell short.
///
/// The counts are not diagnostics for their own sake. `rtl-designdb` writes a
/// database and exits 0 even when elaboration errored, so whether an answer can
/// be trusted is the consumer's to establish, and these are what it reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DbInfo {
    pub schema_version: i64,
    pub tool: String,
    pub tool_version: String,
    pub slang_version: String,
    pub producer_revision: String,
    /// Space-separated names of the elaborated tops. The one part of the seal
    /// that may be absent: a design that elaborates no top has none to name.
    pub top: Option<String>,
    /// `complete | partial | hierarchy_only`. `partial` agrees with the five
    /// counts that cause it: any of them non-zero makes it `partial`, and
    /// `partial` with all five zero is a file whose seal contradicts itself.
    pub analysis_status: String,
    pub error_count: i64,
    pub unresolved_count: i64,
    pub empty_procedure_count: i64,
    pub duplicate_path_count: i64,
    pub recursion_count: i64,
    pub truncated_call_count: i64,
    pub checker_inst_count: i64,
    pub unanalysed_inst_count: i64,
    /// Fingerprints the inputs: two exports with one digest saw the same
    /// filelist, defines and flags.
    pub config_digest: String,
}

/// One level of the elaborated tree: an instance, a generate block, a
/// primitive, an unresolved box, or a package sitting above the roots.
#[derive(Debug, Clone)]
pub struct NodeRow {
    pub node_id: i64,
    pub parent_node_id: Option<i64>,
    pub node_name: String,
    /// `root | instance | generate | primitive | unresolved | package`.
    pub node_kind: String,
    /// Present on the instance-like kinds; a generate level has none of its
    /// own, and a primitive belongs to the instance whose body wrote it.
    pub inst_id: Option<i64>,
    pub parent_inst_id: Option<i64>,
    pub module_name: Option<String>,
    /// A primitive's gate word, or the spelling an unresolved instantiation
    /// could not be found under.
    pub def_name: Option<String>,
    pub file_path: Option<String>,
    pub src_line: Option<u32>,
}

/// One connectable object of one occurrence: a net or a variable.
#[derive(Debug, Clone)]
pub struct NetRow {
    pub net_id: i64,
    pub inst_id: i64,
    /// Dotted and relative to `inst_id`, generate and subroutine segments
    /// included (`g[0].sig`, `bump.v`). The full path is the instance's tree
    /// path plus this — never the scope node's, which repeats the generate
    /// segments this name already carries.
    pub net_name: String,
    pub decl_kind: String,
    pub data_type: Option<String>,
    /// Flattened bit width: the space bit offsets index. NULL for a type with
    /// no bits at all — an event, a string, a class handle.
    pub width: Option<i64>,
    pub file_path: Option<String>,
    pub src_line: Option<u32>,
}

/// One terminal on one occurrence's boundary.
#[derive(Debug, Clone)]
pub struct TerminalRow {
    pub term_id: i64,
    pub inst_id: i64,
    pub term_name: String,
    /// `signal | interface`.
    pub term_kind: String,
    /// NULL on an interface terminal, which has none, and on one of an
    /// unresolved instance, where nobody knows.
    pub direction: Option<String>,
}

/// One statement or statement-level construct.
#[derive(Debug, Clone)]
pub struct StatementRow {
    pub stmt_id: i64,
    pub inst_id: i64,
    /// The level of the tree the statement is written in, which is the
    /// instance unless a generate block encloses it. Three generate instances
    /// of one `always` block share an instance and differ only here.
    pub scope_node_id: i64,
    pub proc_id: Option<i64>,
    /// Execution order within the procedure; NULL outside one, so a continuous
    /// assignment has none.
    pub sequence: Option<i64>,
    /// `assignment | assertion | wait | call | system_task | event_control |
    /// alias | release | trigger | disable`.
    pub stmt_kind: String,
    /// The construct's own word: `always_ff` or `assign` on an assignment,
    /// `force` where one hijacks it, the method's name on a built-in call. An
    /// open vocabulary — the producer may widen it without moving anything.
    pub construct: Option<String>,
    /// `continuous | blocking | nonblocking`, set exactly on assignments.
    pub assign_kind: Option<String>,
    pub delay: Option<String>,
    pub call_site_id: Option<i64>,
    pub branch_id: Option<i64>,
    pub file_path: Option<String>,
    pub src_line: Option<u32>,
}

/// One driving or reading arc of a signal. `v_driver` and `v_load` are the same
/// shape with the ends exchanged, so one row type reads both.
#[derive(Debug, Clone)]
pub struct ArcRow {
    /// The net this arc was asked about, so a batched answer can be sorted
    /// back to the questions that produced it.
    pub signal_net_id: i64,
    /// Which bits of the signal this arc touches.
    pub signal_bits: BitSpan,
    /// The other end, where it is a net this export names.
    pub other_net_id: Option<i64>,
    pub other_inst_id: Option<i64>,
    pub other_name: Option<String>,
    /// Which bits of the far end the arc touches. A window crossing this arc
    /// is rebased onto these.
    pub other_bits: BitSpan,
    /// How the other end was spelled when reached by a hierarchical name.
    pub other_ref: Option<String>,
    /// `driver_kind` or `load_kind`, carried through as the database's own
    /// word rather than folded here.
    pub kind: String,
    /// For a dataflow row, the dependency's own kind, looked up per row.
    pub dep_kind: Option<String>,
    pub dep_id: Option<i64>,
    pub stmt_id: Option<i64>,
    pub conn_id: Option<i64>,
    pub prim_id: Option<i64>,
    pub term_id: Option<i64>,
    pub proc_id: Option<i64>,
    /// Whether the two ends correspond bit for bit. NULL where there is no
    /// second end to correspond with.
    pub map_exact: Option<bool>,
    /// The expansion this row belongs to, where a subroutine body produced it.
    /// A transitive walk that ignores it mixes one call's rows with another's.
    pub call_site_id: Option<i64>,
    pub file_path: Option<String>,
    pub src_line: Option<u32>,
}

/// One level of the gating context a statement sits under.
#[derive(Debug, Clone)]
pub struct BranchRow {
    pub branch_id: i64,
    pub parent_branch_id: Option<i64>,
    pub depth: i64,
    /// Position among siblings, where the level has one. On a case arm it is
    /// the priority — arm *k* is reached only if arms 0..k-1 did not match,
    /// which source line cannot carry since a whole case may be written on
    /// one. An `if` arm and a loop body have no siblings to be ordered among.
    pub ordinal: Option<i64>,
    /// `if | case | case_item | case_default | loop`.
    pub branch_kind: String,
    /// `then | else` on an `if` level: both arms read the same nets, and this
    /// is what separates them.
    pub sense: Option<String>,
    /// The matching semantics on a case point: `case | casez | casex | inside |
    /// matches`.
    pub case_kind: Option<String>,
    /// A `priority`/`unique`/`unique0` qualifier.
    pub check_kind: Option<String>,
    /// The compile-time verdict: 1 this arm runs, 0 it cannot, NULL the
    /// condition is not constant. The rows of a dead arm are kept — the
    /// statement is in the elaborated design — and this is what says so.
    pub static_taken: Option<i64>,
    pub iter_name: Option<String>,
    pub iter_first: Option<i64>,
    pub iter_step: Option<i64>,
    pub iter_count: Option<i64>,
    /// The evaluated label values of a case arm, comma-separated.
    pub labels: Option<String>,
    pub src_line: Option<u32>,
}

/// One reference a statement makes to a net: a target, an operand, or a read
/// classified by role.
#[derive(Debug, Clone)]
pub struct RefRow {
    /// The net, where the reference resolved to one this export names.
    pub net_id: Option<i64>,
    pub net_name: String,
    pub bits: BitSpan,
    /// On a target: `written_by | release_target | alias_binding`. A release
    /// names its lvalue and drives nothing, so the three are not one thing.
    pub kind: Option<String>,
    /// On a gating read, the level it came from.
    pub branch_id: Option<i64>,
}

/// One event a procedure triggers on or waits for.
#[derive(Debug, Clone)]
pub struct EventRow {
    pub proc_kind: String,
    pub net_id: Option<i64>,
    pub net_name: Option<String>,
    /// `sensitivity | wait`.
    pub event_kind: String,
    /// `posedge | negedge | both`, or NULL for a level-sensitive event written
    /// explicitly. The clock is not identified: both events of
    /// `@(posedge clk or negedge rst_n)` are here, and choosing is the reader's.
    pub edge_kind: Option<String>,
}

/// One subroutine-body expansion — that is, one call.
#[derive(Debug, Clone)]
pub struct CallSiteRow {
    pub call_site_id: i64,
    pub parent_call_site_id: Option<i64>,
    pub subroutine_name: String,
    pub depth: i64,
}

fn line_of(v: Option<i64>) -> Option<u32> {
    v.filter(|n| *n > 0).and_then(|n| u32::try_from(n).ok())
}

fn q(e: rusqlite::Error, what: &str) -> String {
    err(format!("reading {what}: {e}"))
}

const DB_INFO_COLS: &str = "schema_version, tool, tool_version, slang_version, \
     producer_revision, top, analysis_status, error_count, unresolved_count, \
     empty_procedure_count, duplicate_path_count, recursion_count, \
     truncated_call_count, checker_inst_count, unanalysed_inst_count, config_digest";

/// The seal, as one row.
///
/// Every column but `top` is required, so a NULL count is a file that is not
/// what it claims rather than a zero — and reading it as zero would report the
/// absence of a seal as an export with nothing wrong in it.
pub fn db_info(c: &Connection) -> Result<DbInfo, String> {
    c.query_row(&format!("SELECT {DB_INFO_COLS} FROM v_db_info"), [], |r| {
        Ok(DbInfo {
            schema_version: r.get(0)?,
            tool: r.get(1)?,
            tool_version: r.get(2)?,
            slang_version: r.get(3)?,
            producer_revision: r.get(4)?,
            top: r.get(5)?,
            analysis_status: r.get(6)?,
            error_count: r.get(7)?,
            unresolved_count: r.get(8)?,
            empty_procedure_count: r.get(9)?,
            duplicate_path_count: r.get(10)?,
            recursion_count: r.get(11)?,
            truncated_call_count: r.get(12)?,
            checker_inst_count: r.get(13)?,
            unanalysed_inst_count: r.get(14)?,
            config_digest: r.get(15)?,
        })
    })
    .map_err(|e| q(e, "v_db_info"))
}

const NODE_COLS: &str = "node_id, parent_node_id, node_name, node_kind, inst_id, \
     parent_inst_id, module_name, def_name, file_path, src_line";

fn node_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<NodeRow> {
    Ok(NodeRow {
        node_id: r.get(0)?,
        parent_node_id: r.get(1)?,
        node_name: r.get(2)?,
        node_kind: r.get(3)?,
        inst_id: r.get(4)?,
        parent_inst_id: r.get(5)?,
        module_name: r.get(6)?,
        def_name: r.get(7)?,
        file_path: r.get(8)?,
        src_line: line_of(r.get(9)?),
    })
}

/// The elaborated tops, in id order. A package is parentless too, and is not
/// one of these.
pub fn roots(c: &Connection) -> Result<Vec<NodeRow>, String> {
    let sql =
        format!("SELECT {NODE_COLS} FROM v_tree_node WHERE node_kind = 'root' ORDER BY node_id");
    let mut stmt = c.prepare(&sql).map_err(|e| q(e, "v_tree_node"))?;
    let rows = stmt.query_map([], node_row).map_err(|e| q(e, "v_tree_node"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_tree_node"))
}

pub fn node(c: &Connection, node_id: i64) -> Result<Option<NodeRow>, String> {
    let sql = format!("SELECT {NODE_COLS} FROM v_tree_node WHERE node_id = ?1");
    c.prepare_cached(&sql).and_then(|mut s| s.query_row([node_id], node_row)).map(Some).or_else(
        |e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            e => Err(q(e, "v_tree_node")),
        },
    )
}

/// One path segment down. Two siblings of one name is a path that has stopped
/// resolving uniquely, and the export counts them: guessing between them would
/// answer about whichever came first.
pub fn child_node(c: &Connection, parent: i64, name: &str) -> Result<Option<NodeRow>, String> {
    let sql =
        format!("SELECT {NODE_COLS} FROM v_tree_node WHERE parent_node_id = ?1 AND node_name = ?2");
    let mut stmt = c.prepare(&sql).map_err(|e| q(e, "v_tree_node"))?;
    let mut found = stmt
        .query_map(rusqlite::params![parent, name], node_row)
        .map_err(|e| q(e, "v_tree_node"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| q(e, "v_tree_node"))?;
    if found.len() > 1 {
        return Err(err(format!("'{name}' names {} siblings; the path is ambiguous", found.len())));
    }
    Ok(found.pop())
}

pub fn children_of(c: &Connection, parent: i64) -> Result<Vec<NodeRow>, String> {
    let sql =
        format!("SELECT {NODE_COLS} FROM v_tree_node WHERE parent_node_id = ?1 ORDER BY node_name");
    let mut stmt = c.prepare(&sql).map_err(|e| q(e, "v_tree_node"))?;
    let rows = stmt.query_map([parent], node_row).map_err(|e| q(e, "v_tree_node"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_tree_node"))
}

const NET_COLS: &str =
    "net_id, inst_id, net_name, decl_kind, data_type, width, file_path, src_line";

fn net_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<NetRow> {
    Ok(NetRow {
        net_id: r.get(0)?,
        inst_id: r.get(1)?,
        net_name: r.get(2)?,
        decl_kind: r.get(3)?,
        data_type: r.get(4)?,
        width: r.get(5)?,
        file_path: r.get(6)?,
        src_line: line_of(r.get(7)?),
    })
}

pub fn net_by_name(c: &Connection, inst: i64, name: &str) -> Result<Option<NetRow>, String> {
    let sql = format!("SELECT {NET_COLS} FROM v_net WHERE inst_id = ?1 AND net_name = ?2");
    c.query_row(&sql, rusqlite::params![inst, name], net_row).map(Some).or_else(|e| match e {
        rusqlite::Error::QueryReturnedNoRows => Ok(None),
        e => Err(q(e, "v_net")),
    })
}

/// Every net of one occurrence, by name. What a caller offers when a name did
/// not resolve.
pub fn nets_of_instance(c: &Connection, inst: i64) -> Result<Vec<NetRow>, String> {
    let sql = format!("SELECT {NET_COLS} FROM v_net WHERE inst_id = ?1 ORDER BY net_name");
    let mut stmt = c.prepare(&sql).map_err(|e| q(e, "v_net"))?;
    let rows = stmt.query_map([inst], net_row).map_err(|e| q(e, "v_net"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_net"))
}

const TERM_COLS: &str = "term_id, inst_id, term_name, term_kind, direction";

fn term_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<TerminalRow> {
    Ok(TerminalRow {
        term_id: r.get(0)?,
        inst_id: r.get(1)?,
        term_name: r.get(2)?,
        term_kind: r.get(3)?,
        direction: r.get(4)?,
    })
}

/// An interface port of one occurrence. Such a port is a scope in a path but a
/// terminal here, so resolving a member reference detours through it.
pub fn interface_terminal(
    c: &Connection,
    inst: i64,
    name: &str,
) -> Result<Option<TerminalRow>, String> {
    let sql = format!(
        "SELECT {TERM_COLS} FROM v_term \
         WHERE inst_id = ?1 AND term_name = ?2 AND term_kind = 'interface'"
    );
    c.query_row(&sql, rusqlite::params![inst, name], term_row).map(Some).or_else(|e| match e {
        rusqlite::Error::QueryReturnedNoRows => Ok(None),
        e => Err(q(e, "v_term")),
    })
}

/// Whether `name` is a modport of the interface `inst` is an instance of.
///
/// A modport is a view of an interface's nets, so a path may name one where no
/// scope of that name exists. Nothing records the declaration: the only trace
/// of a modport is the `modport` a port that views it carries, so a view no
/// port anywhere takes on this interface is one this cannot recognise. The
/// unit is the interface's module, not this instance, because a modport
/// belongs to the interface rather than to one elaboration of it.
pub fn names_modport(c: &Connection, inst: i64, name: &str) -> Result<bool, String> {
    c.query_row(
        "SELECT EXISTS(SELECT 1 FROM net_conn c \
           JOIN term t ON t.id = c.term_id \
           JOIN inst i ON i.id = c.outer_intf_inst_id \
          WHERE t.modport = ?2 \
            AND i.module_id = (SELECT module_id FROM inst WHERE id = ?1))",
        rusqlite::params![inst, name],
        |r| r.get::<_, i64>(0),
    )
    .map(|n| n != 0)
    .map_err(|e| q(e, "net_conn"))
}

/// The interfaces a terminal is bound to, in declaration order.
///
/// Empty is no interface binding. `None` in a slot is a binding the export
/// could put no occurrence against — a synthesized shape — and is not somewhere
/// to continue. An ARRAY port has one slot per element, each naming the element
/// that segment binds, so more than one answer here is a path that has not said
/// which element it means.
pub fn iface_targets(c: &Connection, term_id: i64) -> Result<Vec<Option<i64>>, String> {
    let mut stmt = c
        .prepare_cached(
            "SELECT outer_intf_inst_id FROM v_net_conn \
              WHERE term_id = ?1 AND conn_kind = 'interface' ORDER BY ordinal",
        )
        .map_err(|e| q(e, "v_net_conn"))?;
    stmt.query_map([term_id], |r| r.get::<_, Option<i64>>(0))
        .map_err(|e| q(e, "v_net_conn"))?
        .collect::<Result<_, _>>()
        .map_err(|e| q(e, "v_net_conn"))
}

const STMT_COLS: &str = "stmt_id, inst_id, scope_node_id, proc_id, sequence, stmt_kind, \
     construct, assign_kind, delay, call_site_id, branch_id, file_path, src_line";

fn stmt_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<StatementRow> {
    Ok(StatementRow {
        stmt_id: r.get(0)?,
        inst_id: r.get(1)?,
        scope_node_id: r.get(2)?,
        proc_id: r.get(3)?,
        sequence: r.get(4)?,
        stmt_kind: r.get(5)?,
        construct: r.get(6)?,
        assign_kind: r.get(7)?,
        delay: r.get(8)?,
        call_site_id: r.get(9)?,
        branch_id: r.get(10)?,
        file_path: r.get(11)?,
        src_line: line_of(r.get(12)?),
    })
}

pub fn statement(c: &Connection, stmt_id: i64) -> Result<Option<StatementRow>, String> {
    let sql = format!("SELECT {STMT_COLS} FROM v_stmt WHERE stmt_id = ?1");
    c.prepare_cached(&sql).and_then(|mut s| s.query_row([stmt_id], stmt_row)).map(Some).or_else(
        |e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            e => Err(q(e, "v_stmt")),
        },
    )
}

/// Which way an arc points, and so which of the two views carries it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Driver,
    Load,
}

impl Direction {
    pub fn tag(self) -> &'static str {
        match self {
            Direction::Driver => "driver",
            Direction::Load => "load",
        }
    }
}

/// Every recorded arc of one net, in the given direction.
pub fn arcs(c: &Connection, net: i64, dir: Direction) -> Result<Vec<ArcRow>, String> {
    arcs_of(c, &[net], dir)
}

/// The batch sizes a query is padded up to.
///
/// One statement per net costs three times what one statement per five hundred
/// does — measured, on a design whose cone reaches six thousand — and a walk
/// asks about a whole breadth-first level at once. Padding to a few fixed
/// widths keeps the SQL text constant, so SQLite's own statement cache still
/// holds it; a size that varied with the level would miss every time.
const BATCH_SIZES: [usize; 4] = [1, 16, 128, 512];

/// Every recorded arc of each of `nets`, in the given direction.
///
/// `v_driver` and `v_load` differ in which end they call the signal, so the
/// column lists are written out separately and read into one row shape.
pub fn arcs_of(c: &Connection, nets: &[i64], dir: Direction) -> Result<Vec<ArcRow>, String> {
    let mut out = Vec::new();
    let mut rest = nets;
    while !rest.is_empty() {
        let width = *BATCH_SIZES.iter().find(|w| **w >= rest.len()).unwrap_or(&512);
        let take = width.min(rest.len());
        let (batch, tail) = rest.split_at(take);
        rest = tail;
        // Padded with an id no row carries, so one prepared statement serves
        // every batch of this width.
        let mut ids: Vec<i64> = batch.to_vec();
        ids.resize(width, -1);
        out.extend(arcs_batch(c, &ids, dir)?);
    }
    Ok(out)
}

fn arcs_batch(c: &Connection, ids: &[i64], dir: Direction) -> Result<Vec<ArcRow>, String> {
    let sql = match dir {
        Direction::Driver => {
            "SELECT signal_net_id, signal_lo, signal_hi, signal_exact, \
                    driver_net_id, driver_inst_id, driver_name, \
                    driver_lo, driver_hi, driver_exact, driver_ref, \
                    driver_kind, dep_id, stmt_id, conn_id, prim_id, term_id, NULL, \
                    map_exact, call_site_id, file_path, src_line \
             FROM v_driver WHERE signal_net_id IN "
        }
        Direction::Load => {
            "SELECT signal_net_id, signal_lo, signal_hi, signal_exact, \
                    load_net_id, load_inst_id, load_name, \
                    load_lo, load_hi, load_exact, load_ref, \
                    load_kind, dep_id, stmt_id, conn_id, NULL, term_id, proc_id, \
                    map_exact, call_site_id, file_path, src_line \
             FROM v_load WHERE signal_net_id IN "
        }
    };
    let places = format!("({})", vec!["?"; ids.len()].join(","));
    let mut stmt =
        c.prepare_cached(&(sql.to_string() + &places)).map_err(|e| q(e, "v_driver/v_load"))?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(ids), |r| {
            Ok(ArcRow {
                signal_net_id: r.get(0)?,
                signal_bits: BitSpan::read(r.get(1)?, r.get(2)?, r.get(3)?),
                other_net_id: r.get(4)?,
                other_inst_id: r.get(5)?,
                other_name: r.get(6)?,
                other_bits: BitSpan::read(r.get(7)?, r.get(8)?, r.get(9)?),
                other_ref: r.get(10)?,
                kind: r.get(11)?,
                dep_kind: None,
                dep_id: r.get(12)?,
                stmt_id: r.get(13)?,
                conn_id: r.get(14)?,
                prim_id: r.get(15)?,
                term_id: r.get(16)?,
                proc_id: r.get(17)?,
                map_exact: r.get::<_, Option<i64>>(18)?.map(|v| v != 0),
                call_site_id: r.get(19)?,
                file_path: r.get(20)?,
                src_line: line_of(r.get(21)?),
            })
        })
        .map_err(|e| q(e, "v_driver/v_load"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_driver/v_load"))
}

/// The dependency kind behind one dataflow arc.
///
/// Looked up per row rather than joined: `v_load LEFT JOIN v_net_dep` reads
/// naturally and makes SQLite materialise both views. There are as many of
/// these lookups as there are dataflow rows, which is as many as there are
/// answers.
pub fn dep_kind(c: &Connection, dep_id: i64) -> Result<Option<(String, Option<i64>)>, String> {
    c.prepare_cached("SELECT dep_kind, prim_id FROM v_net_dep WHERE dep_id = ?1")
        .and_then(|mut s| s.query_row([dep_id], |r| Ok((r.get(0)?, r.get(1)?))))
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            e => Err(q(e, "v_net_dep")),
        })
}

/// The statement's right-hand-side reads, in written order.
pub fn operands_of(c: &Connection, stmt_id: i64) -> Result<Vec<RefRow>, String> {
    let mut stmt = c
        .prepare(
            "SELECT net_id, net_name, lo, hi, is_exact \
             FROM v_stmt_operand WHERE stmt_id = ?1 ORDER BY ordinal",
        )
        .map_err(|e| q(e, "v_stmt_operand"))?;
    let rows = stmt
        .query_map([stmt_id], |r| {
            Ok(RefRow {
                net_id: r.get(0)?,
                net_name: r.get(1)?,
                bits: BitSpan::read(r.get(2)?, r.get(3)?, r.get(4)?),
                kind: None,
                branch_id: None,
            })
        })
        .map_err(|e| q(e, "v_stmt_operand"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_stmt_operand"))
}

/// The nets a statement's gating levels read, each tagged with the level it
/// was read at.
///
/// v20 keeps a condition's reads on its level: `branch_ref` for a net this
/// instance names, and a `hier_ref` keyed on the level — `stmt_id` NULL — for
/// a condition reaching outside it (`if (top.dbg_en)`). Reading only one
/// would report an externally gated statement as gated by nothing, so both
/// are fetched and the level's id ties each read to the gate that made it.
pub fn gating_reads(c: &Connection, stmt_id: i64) -> Result<Vec<RefRow>, String> {
    let Some(stmt) = statement(c, stmt_id)? else { return Ok(Vec::new()) };
    let Some(branch_id) = stmt.branch_id else { return Ok(Vec::new()) };
    let chain = branch_chain(c, branch_id)?;
    let ids: Vec<i64> = chain.iter().map(|b| b.branch_id).collect();
    if ids.is_empty() {
        return Ok(Vec::new());
    }
    let places = format!("({})", vec!["?"; ids.len()].join(","));

    let mut out = Vec::new();

    // A net this instance names: branch_ref rows.
    let sql = format!(
        "SELECT r.net_id, n.name, r.lo, r.hi, r.is_exact, r.branch_id \
           FROM branch_ref r JOIN net n ON n.id = r.net_id \
          WHERE r.branch_id IN {places}"
    );
    let mut stmt = c.prepare_cached(&sql).map_err(|e| q(e, "branch_ref"))?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(&ids), |r| {
            Ok(RefRow {
                net_id: r.get::<_, Option<i64>>(0)?,
                net_name: r.get(1)?,
                bits: BitSpan::read(r.get(2)?, r.get(3)?, r.get(4)?),
                kind: None,
                branch_id: r.get(5)?,
            })
        })
        .map_err(|e| q(e, "branch_ref"))?;
    out.extend(rows.collect::<Result<_, _>>().map_err(|e| q(e, "branch_ref"))?);

    // A condition reaching outside the instance: hier_ref rows keyed on the
    // level. An upward reference resolves to no net of this export, so the
    // path it was written as is the only name there is.
    let sql = format!(
        "SELECT h.resolved_net_id, h.path, h.lo, h.hi, h.is_exact, h.branch_id \
           FROM hier_ref h \
          WHERE h.branch_id IN {places} AND h.access = 'read'"
    );
    let mut stmt = c.prepare_cached(&sql).map_err(|e| q(e, "hier_ref"))?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(&ids), |r| {
            Ok(RefRow {
                net_id: r.get::<_, Option<i64>>(0)?,
                net_name: r.get(1)?,
                bits: BitSpan::read(r.get(2)?, r.get(3)?, r.get(4)?),
                kind: None,
                branch_id: r.get(5)?,
            })
        })
        .map_err(|e| q(e, "hier_ref"))?;
    out.extend(rows.collect::<Result<_, _>>().map_err(|e| q(e, "hier_ref"))?);

    Ok(out)
}

const BRANCH_COLS: &str = "branch_id, parent_branch_id, depth, ordinal, branch_kind, sense, \
     case_kind, check_kind, static_taken, iter_name, iter_first, iter_step, iter_count, \
     labels, src_line";

fn branch_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<BranchRow> {
    Ok(BranchRow {
        branch_id: r.get(0)?,
        parent_branch_id: r.get(1)?,
        depth: r.get(2)?,
        ordinal: r.get(3)?,
        branch_kind: r.get(4)?,
        sense: r.get(5)?,
        case_kind: r.get(6)?,
        check_kind: r.get(7)?,
        static_taken: r.get(8)?,
        iter_name: r.get(9)?,
        iter_first: r.get(10)?,
        iter_step: r.get(11)?,
        iter_count: r.get(12)?,
        labels: r.get(13)?,
        src_line: line_of(r.get(14)?),
    })
}

/// The gating levels over a statement, outermost first.
///
/// One point query per level: the chain is walked here rather than in a
/// recursive CTE, for the reason every closure is — SQLite cannot push the
/// recursion's current value into a compound view, and the depth of a gating
/// chain is small.
pub fn branch_chain(c: &Connection, branch_id: i64) -> Result<Vec<BranchRow>, String> {
    let sql = format!("SELECT {BRANCH_COLS} FROM v_branch WHERE branch_id = ?1");
    let mut stmt = c.prepare(&sql).map_err(|e| q(e, "v_branch"))?;

    let mut chain = Vec::new();
    let mut at = Some(branch_id);
    while let Some(id) = at {
        let row = match stmt.query_row([id], branch_row) {
            Ok(row) => row,
            Err(rusqlite::Error::QueryReturnedNoRows) => break,
            Err(e) => return Err(q(e, "v_branch")),
        };
        at = row.parent_branch_id;
        chain.push(row);
    }
    chain.reverse();
    Ok(chain)
}

/// The events a procedure triggers on.
pub fn events_of_procedure(c: &Connection, proc_id: i64) -> Result<Vec<EventRow>, String> {
    let mut stmt = c
        .prepare(
            "SELECT proc_kind, net_id, net_name, event_kind, edge_kind \
             FROM v_proc_event WHERE proc_id = ?1 AND event_kind = 'sensitivity' \
             ORDER BY proc_event_id",
        )
        .map_err(|e| q(e, "v_proc_event"))?;
    let rows = stmt
        .query_map([proc_id], |r| {
            Ok(EventRow {
                proc_kind: r.get(0)?,
                net_id: r.get(1)?,
                net_name: r.get(2)?,
                event_kind: r.get(3)?,
                edge_kind: r.get(4)?,
            })
        })
        .map_err(|e| q(e, "v_proc_event"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_proc_event"))
}

/// A procedure's kind, for a statement that names one.
pub fn proc_kind(c: &Connection, proc_id: i64) -> Result<Option<String>, String> {
    c.query_row("SELECT proc_kind FROM proc WHERE id = ?1", [proc_id], |r| r.get(0))
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            e => Err(q(e, "proc")),
        })
}

/// The call string a row belongs to, outermost first.
///
/// A body is walked once per call site and its formals are shared, so this is
/// what tells one call's rows from another's.
pub fn call_chain(c: &Connection, call_site_id: i64) -> Result<Vec<CallSiteRow>, String> {
    let mut stmt = c
        .prepare(
            "SELECT call_site_id, parent_call_site_id, subroutine_name, depth \
             FROM v_call_site WHERE call_site_id = ?1",
        )
        .map_err(|e| q(e, "v_call_site"))?;

    let mut chain = Vec::new();
    let mut at = Some(call_site_id);
    while let Some(id) = at {
        let row = match stmt.query_row([id], |r| {
            Ok(CallSiteRow {
                call_site_id: r.get(0)?,
                parent_call_site_id: r.get(1)?,
                subroutine_name: r.get(2)?,
                depth: r.get(3)?,
            })
        }) {
            Ok(row) => row,
            Err(rusqlite::Error::QueryReturnedNoRows) => break,
            Err(e) => return Err(q(e, "v_call_site")),
        };
        at = row.parent_call_site_id;
        chain.push(row);
    }
    chain.reverse();
    Ok(chain)
}

/// A net, by id. What a walk needs once it has arrived somewhere by an arc
/// rather than by a name.
pub fn net_of(c: &Connection, net_id: i64) -> Result<Option<NetRow>, String> {
    let sql = format!("SELECT {NET_COLS} FROM v_net WHERE net_id = ?1");
    c.prepare_cached(&sql).and_then(|mut s| s.query_row([net_id], net_row)).map(Some).or_else(|e| {
        match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            e => Err(q(e, "v_net")),
        }
    })
}

/// Every net a state element writes, and every net one of those is wired to.
///
/// This is what a combinational walk stops at, and it is computed once for the
/// database rather than asked per net. Two reasons. A port ties two names to
/// one electrical node, so a flop's output is a state element under the child's
/// name and the parent's alike, and finding that out per net means following
/// the crossings from each — a query per net where the whole relation is two
/// scans. And a cone asks about every net it reaches, so per-net would be the
/// walk's dominant cost while the answer does not depend on the walk at all.
///
/// A bare `always` is clocked when its sensitivity names an edge. Which edge is
/// the clock is not decided here, and does not need to be.
/// Every branch a constant condition rules out, and every branch below one.
///
/// Read once for the database rather than a chain walk per arc: a cone crosses
/// hundreds of thousands of arcs and a design has a few thousand branches. A
/// statement under any of these is in the design and cannot run at this
/// parameterisation, which is what `trace` calls unreachable.
pub fn dead_branches(c: &Connection) -> Result<HashSet<i64>, String> {
    const SQL: &str = "WITH RECURSIVE dead(branch_id) AS ( \
            SELECT branch_id FROM v_branch WHERE static_taken = 0 \
          UNION \
            SELECT b.branch_id FROM v_branch b \
              JOIN dead d ON b.parent_branch_id = d.branch_id) \
        SELECT branch_id FROM dead";
    let mut stmt = c.prepare(SQL).map_err(|e| q(e, "v_branch"))?;
    let rows = stmt.query_map([], |r| r.get::<_, i64>(0)).map_err(|e| q(e, "v_branch"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_branch"))
}

pub fn state_elements(c: &Connection) -> Result<(HashSet<i64>, HashSet<i64>), String> {
    // An edge-triggered procedure stores what it assigns non-blockingly; a
    // blocking assignment in one is a temporary computed within the same
    // evaluation, and reading it as storage ends a combinational cone at a
    // value that never held one. A latch's assignments are blocking by nature,
    // so there the kind says nothing.
    const WRITTEN: &str = "SELECT DISTINCT d.tgt_net_id, p.proc_kind \
         FROM net_dep d \
           JOIN stmt s ON s.id = d.stmt_id \
           JOIN proc p ON p.id = s.proc_id \
        WHERE d.dep_kind = 'data' \
          AND (p.proc_kind = 'always_latch' \
               OR (s.assign_kind = 'nonblocking' \
                   AND (p.proc_kind = 'always_ff' \
                        OR (p.proc_kind = 'always' AND EXISTS(\
                             SELECT 1 FROM proc_event e \
                              WHERE e.proc_id = p.id AND e.event_kind = 'sensitivity' \
                                AND e.edge_kind IS NOT NULL)))))";
    let mut stmt = c.prepare(WRITTEN).map_err(|e| q(e, "net_dep"))?;
    let mut clocked: HashSet<i64> = HashSet::new();
    let mut latch: HashSet<i64> = HashSet::new();
    let rows = stmt
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))
        .map_err(|e| q(e, "net_dep"))?;
    for row in rows {
        let (net, kind) = row.map_err(|e| q(e, "net_dep"))?;
        // `always_latch` holds a level; the other two run on an edge.
        if kind == "always_latch" {
            latch.insert(net)
        } else {
            clocked.insert(net)
        };
    }

    // The crossings that carry the whole object, as one sorted array of both
    // directions rather than a map of vectors: a design has tens of thousands
    // of them, and the lookup is a binary search into one allocation instead of
    // one allocation each.
    //
    // Only whole-width ties propagate. A port wired to four bits of a net makes
    // those four a flop's output and says nothing about the other four, so
    // carrying the property across would call a half-combinational net a state
    // element and end every combinational cone through it.
    let mut stmt = c
        .prepare(
            "SELECT signal_net_id, driver_net_id FROM v_driver \
              WHERE driver_kind = 'connection' AND driver_net_id IS NOT NULL \
                AND signal_lo IS NULL AND driver_lo IS NULL",
        )
        .map_err(|e| q(e, "v_driver"))?;
    let rows = stmt
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))
        .map_err(|e| q(e, "v_driver"))?;
    let mut wired: Vec<(i64, i64)> = Vec::new();
    for row in rows {
        let (a, b) = row.map_err(|e| q(e, "v_driver"))?;
        wired.push((a, b));
        wired.push((b, a));
    }
    wired.sort_unstable();

    let neighbours = |net: i64| -> &[(i64, i64)] {
        let lo = wired.partition_point(|(a, _)| *a < net);
        let hi = wired.partition_point(|(a, _)| *a <= net);
        &wired[lo..hi]
    };
    for set in [&mut clocked, &mut latch] {
        let mut frontier: Vec<i64> = set.iter().copied().collect();
        while let Some(net) = frontier.pop() {
            for (_, far) in neighbours(net) {
                if set.insert(*far) {
                    frontier.push(*far);
                }
            }
        }
    }
    Ok((clocked, latch))
}

/// How many nets one level of the tree declares.
///
/// By scope, not by instance: a generate block declares nets of its own, and
/// they belong to the instance around it. Counting by instance would give the
/// generate level nothing and the instance everything, saying of both the
/// opposite of what is there.
pub fn net_count(c: &Connection, node: i64) -> Result<i64, String> {
    c.prepare_cached("SELECT count(*) FROM net WHERE scope_node_id = ?1")
        .and_then(|mut s| s.query_row([node], |r| r.get(0)))
        .map_err(|e| q(e, "net"))
}

/// Nets whose name matches a glob, anywhere in the design.
///
/// Matching is on the name relative to an instance, which is what the index
/// holds; a caller wanting to match a full path filters the answers. `limit`
/// bounds the query itself rather than the answer — a pattern of `*` on a large
/// design would otherwise read every net into memory to show twenty.
pub fn nets_matching(
    c: &Connection,
    pattern: &str,
    limit: usize,
) -> Result<(Vec<NetRow>, bool), String> {
    let sql =
        format!("SELECT {NET_COLS} FROM v_net WHERE net_name GLOB ?1 ORDER BY net_id LIMIT ?2");
    let mut stmt = c.prepare_cached(&sql).map_err(|e| q(e, "v_net"))?;
    let rows = stmt
        .query_map(rusqlite::params![pattern, limit as i64], net_row)
        .map_err(|e| q(e, "v_net"))?;
    let found: Vec<NetRow> = rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_net"))?;
    let hit_cap = found.len() >= limit;
    Ok((found, hit_cap))
}

/// Tree levels whose own segment matches a glob.
pub fn nodes_matching(
    c: &Connection,
    pattern: &str,
    limit: usize,
) -> Result<(Vec<NodeRow>, bool), String> {
    let sql = format!(
        "SELECT {NODE_COLS} FROM v_tree_node WHERE node_name GLOB ?1 ORDER BY node_id LIMIT ?2"
    );
    let mut stmt = c.prepare_cached(&sql).map_err(|e| q(e, "v_tree_node"))?;
    let rows = stmt
        .query_map(rusqlite::params![pattern, limit as i64], node_row)
        .map_err(|e| q(e, "v_tree_node"))?;
    let found: Vec<NodeRow> = rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_tree_node"))?;
    let hit_cap = found.len() >= limit;
    Ok((found, hit_cap))
}

/// A definition's name, what kind of definition it is, and how many times it
/// elaborated.
pub type ModuleMatch = (String, String, i64);

/// Definitions whose name matches a glob, with how many times each elaborated.
pub fn modules_matching(
    c: &Connection,
    pattern: &str,
    limit: usize,
) -> Result<(Vec<ModuleMatch>, bool), String> {
    let mut stmt = c
        .prepare_cached(
            "SELECT m.name, m.def_kind, count(i.id) FROM module m \
               LEFT JOIN inst i ON i.module_id = m.id \
              WHERE m.name GLOB ?1 GROUP BY m.id ORDER BY m.name LIMIT ?2",
        )
        .map_err(|e| q(e, "module"))?;
    let rows = stmt
        .query_map(rusqlite::params![pattern, limit as i64], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })
        .map_err(|e| q(e, "module"))?;
    let found: Vec<ModuleMatch> = rows.collect::<Result<_, _>>().map_err(|e| q(e, "module"))?;
    let hit_cap = found.len() >= limit;
    Ok((found, hit_cap))
}

/// The statements of one procedure that write one net, in execution order,
/// each with the bits it writes.
///
/// Which of several assignments is in effect is a matter of the order they run
/// in, the conditions each sits under, and which bits each touches — two
/// statements writing disjoint halves do not overwrite one another at all. The
/// order is `sequence`; deciding what it means is the reader's.
///
/// Two sources, because a write can leave the instance: `u.n = a` inside a
/// procedure is a `hier_ref` with `access='write'` and no `stmt_target` row at
/// all, so asking only the first would report a procedure that writes a signal
/// twice as writing it never.
pub fn procedure_writes(
    c: &Connection,
    proc_id: i64,
    net: i64,
) -> Result<Vec<(StatementRow, BitSpan)>, String> {
    // Qualified: the joined views share several column names, and an
    // unqualified list would be ambiguous where they overlap.
    let cols = STMT_COLS.split(", ").map(|c| format!("s.{c}")).collect::<Vec<_>>().join(", ");
    // Where the statement's own columns end and the target's begin. Counted
    // rather than written down: a column added to the list would otherwise
    // shift the three that follow it and be read as bits.
    let after: usize = STMT_COLS.split(", ").count();
    let sql = format!(
        "SELECT {cols}, t.lo, t.hi, t.is_exact FROM v_stmt s \
           JOIN v_stmt_target t ON t.stmt_id = s.stmt_id \
          WHERE s.proc_id = ?1 AND t.net_id = ?2 AND t.target_kind = 'written_by' \
          UNION ALL \
         SELECT {cols}, h.lo, h.hi, h.is_exact FROM v_stmt s \
           JOIN hier_ref h ON h.stmt_id = s.stmt_id \
          WHERE s.proc_id = ?1 AND h.resolved_net_id = ?2 AND h.access = 'write' \
          ORDER BY sequence"
    );
    let mut stmt = c.prepare_cached(&sql).map_err(|e| q(e, "v_stmt"))?;
    let rows = stmt
        .query_map(rusqlite::params![proc_id, net], |r| {
            let bits = BitSpan::read(r.get(after)?, r.get(after + 1)?, r.get(after + 2)?);
            Ok((stmt_row(r)?, bits))
        })
        .map_err(|e| q(e, "v_stmt"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_stmt"))
}

/// Which expansion encloses which, for every call in the design.
///
/// Read whole rather than chased per row: a design has a handful of call sites
/// where a cone has thousands of arcs, and the chain is what tells a nested
/// call from a sibling one.
pub fn call_parents(c: &Connection) -> Result<std::collections::HashMap<i64, Option<i64>>, String> {
    let mut stmt = c
        .prepare("SELECT call_site_id, parent_call_site_id FROM v_call_site")
        .map_err(|e| q(e, "v_call_site"))?;
    let rows = stmt
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?)))
        .map_err(|e| q(e, "v_call_site"))?;
    rows.collect::<Result<_, _>>().map_err(|e| q(e, "v_call_site"))
}

/// Whether this net is a subroutine's own — a formal or a local.
///
/// Those are the nets one call shares with another, and the only ones a walk
/// has to keep calls apart at. The test is the name: a subroutine's nets are
/// named through it (`put.d`), and a module-level net cannot be, since one
/// spelled with a dot is an escaped identifier and keeps its backslash.
///
/// Asking instead whether every row touching the net carries a call tag looks
/// equivalent and is not: a module variable a subroutine body happens to be the
/// only writer of has no untagged row either, and treating it as a formal makes
/// the walk refuse its own siblings' rows — which reads as no path where there
/// plainly is one.
pub fn is_body_local(c: &Connection, net: i64) -> Result<bool, String> {
    c.query_row(
        "SELECT EXISTS(\
             SELECT 1 FROM net n JOIN call_site cs ON cs.inst_id = n.inst_id \
              WHERE n.id = ?1 AND n.name LIKE cs.subroutine_name || '.%')",
        [net],
        |r| r.get::<_, i64>(0),
    )
    .map(|v| v != 0)
    .map_err(|e| q(e, "net"))
}

/// Where a source file actually is, and what it hashed to.
///
/// Rows carry the spelling from the filelist, which resolves against a working
/// directory nothing records; `src_file` carries the absolute path the export
/// read. The two answer different questions — one is for showing, this one is
/// for opening.
pub fn source_file(c: &Connection, file_path: &str) -> Result<Option<(String, String)>, String> {
    c.query_row(
        "SELECT s.path, s.digest FROM file f JOIN src_file s ON s.id = f.src_file_id \
         WHERE f.path = ?1",
        [file_path],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .map(Some)
    .or_else(|e| match e {
        rusqlite::Error::QueryReturnedNoRows => Ok(None),
        e => Err(q(e, "file")),
    })
}

/// References the export wrote as a path and could not resolve, as
/// (reads, writes).
///
/// Not in the seal. A read that did not resolve leaves a net with fewer
/// sources than it has; a write that did not resolve leaves one reading as
/// undriven while something plainly writes it — and that second case is
/// indistinguishable, from the outside, from a net nothing drives.
pub fn unresolved_refs(c: &Connection) -> Result<(i64, i64), String> {
    const SQL: &str = "SELECT \
            COALESCE(SUM(access = 'read'), 0), COALESCE(SUM(access = 'write'), 0) \
          FROM v_hier_ref WHERE resolved_net_id IS NULL";
    c.query_row(SQL, [], |r| Ok((r.get(0)?, r.get(1)?))).map_err(|e| q(e, "v_hier_ref"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Db;
    use crate::open::fixture::{tmp, write_db};

    #[test]
    fn the_seal_reads_back_with_its_counts() {
        let dir = tmp("dbinfo");
        let path = dir.join("design.db");
        write_db(
            &path,
            crate::SCHEMA_VERSION,
            &["UPDATE db_info SET top = 'dut', analysis_status = 'partial', \
               error_count = 4, empty_procedure_count = 2"],
        );
        let db = Db::open(&path).unwrap();
        let info = db_info(db.conn()).unwrap();

        assert_eq!(info.schema_version, crate::SCHEMA_VERSION);
        assert_eq!(info.top.as_deref(), Some("dut"));
        assert_eq!(info.analysis_status, "partial");
        assert_eq!(info.error_count, 4);
        assert_eq!(info.empty_procedure_count, 2);
        assert_eq!(info.unresolved_count, 0);
    }

    #[test]
    fn a_database_without_the_seal_row_is_refused_not_read_as_zeros() {
        let dir = tmp("dbinfo-hollow");
        let path = dir.join("design.db");
        // v20 stores the seal as one required row, so a database with no
        // `db_info` row fails the version gate at open rather than being read
        // as an export with nothing wrong in it.
        write_db(&path, crate::SCHEMA_VERSION, &["DELETE FROM db_info"]);
        let Err(e) = Db::open(&path) else { panic!("a database with no seal was opened") };
        assert!(matches!(e, crate::OpenError::NoSchemaVersion { .. }), "expected NoSchemaVersion, got {e}");
    }
}


