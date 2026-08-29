//! RTLScanner — signal trace, driver and load analysis over a design database.
//!
//! The RTL is elaborated once, by `rtl-designdb`, into rows this tool queries.
//! Structure comes from those rows and from nowhere else — this tool reads no
//! RTL beyond quoting a line it can verify, and no waveform — so what it
//! reports is what the export recorded, at the precision it recorded it.

mod browse;
mod cone;
mod cone_result;
mod envelope;
mod info;
mod trace;

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use designdb::{Db, Direction, bits, resolve, schema};
use serde_json::json;

use envelope::{CommandError, Diagnostic, ErrorCode, Rendered};

/// The help, written by hand: one text for `-h` and `--help` alike, grouped
/// by the command an option belongs to.
const HELP: &str = concat!(
    "rtlscanner ",
    env!("CARGO_PKG_VERSION"),
    " — a signal-level tracer for RTL debug

Usage: rtlscanner <command> <db> [arguments] [options]

Commands:
  info    <db>             whether this database can be trusted — run first
  tree    <db> [SCOPE]     what the design is made of, level by level
  find    <db> PATTERN     where a name lives (PATTERN is a glob: *, ?)
  trace   <db> SIGNAL      what drives SIGNAL, one hop out
  fanin   <db> SIGNAL      everything SIGNAL depends on, transitively
  fanout  <db> SIGNAL      everything that depends on SIGNAL, transitively
  path    <db> FROM TO     a route from FROM to TO

<db> is a design database, exported from the RTL once by rtl-designdb.
SIGNAL, FROM and TO are hierarchical paths like top.u_core.status[3].

Options (all optional, under the commands that take them):
  every command except info
    --top NAME           which top, where the design has several
  trace, tree, fanin, fanout, path
    --anchor PATH        where this design's root sits in your paths, e.g.
                         tb.u_dut. Worked out from the path when omitted, so
                         a waveform's spelling is accepted as it comes.
  trace
    --load               what reads the signal, instead of what drives it
    --ctl                include the conditions gating each statement
  fanin, fanout, path
    --depth N            stop after N hops; 0 = unbounded
                         (default 4; path and --comb are unbounded)
    --comb               stop at state elements: this cycle's logic only
    --through-latch      under --comb, cross latches anyway
    --no-ctl             leave out gating conditions entirely
    --follow-ctl         follow a gating condition on, rather than naming it
                         and stopping there, which is the default
  tree
    --depth N            levels shown below SCOPE; 0 = all (default 3)
  find
    --instances | --modules
                         search instances, or definitions, instead of nets
  info, tree, find, fanin, fanout
    --limit N            rows shown; 0 = all (default 200)
  every command
    --json               emit the JSON envelope instead of a terminal view

  -h, --help             print this help
  -V, --version          print version

Environment:
  RTLSCANNER_MAX_NODES   nets a fanin/fanout/path walk may reach before it
                         stops and says so (default 100000; 0 = no bound).
                         This bounds the WALK; --limit bounds the answer.

Make the database first, from the RTL, with RTLDebugDBKit's exporter:
  rtl-designdb <sources...> --top NAME -o design.db
This version reads database schema v20: the exporter and this tool must
agree on the schema version.
The database is a snapshot of the RTL at export time; edit the source
and it is stale until re-exported.
"
);

#[derive(Parser)]
#[command(name = "rtlscanner", version, disable_help_subcommand = true, override_help = HELP)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Report what a design database is and how far it can be trusted
    Info {
        /// Path to the design database
        db: PathBuf,
        /// List at most this many unchanged sources; 0 for all
        #[arg(long)]
        limit: Option<i64>,
        /// Emit the JSON envelope rather than a terminal view
        #[arg(long)]
        json: bool,
    },
    /// What drives a signal, or what reads it, one hop out
    Trace(TraceArgs),
    /// Everything a signal depends on, transitively
    Fanin(ConeArgs),
    /// Everything that depends on a signal, transitively
    Fanout(ConeArgs),
    /// A route from one signal to another
    Path(PathArgs),
    /// What the design is made of, level by level
    Tree(TreeArgs),
    /// Where a name lives
    Find(FindArgs),
}

#[derive(Args)]
struct TreeArgs {
    #[command(flatten)]
    common: Common,
    #[command(flatten)]
    at: AnchorArg,
    /// Start below this scope rather than at the top
    scope: Option<String>,
    /// Show this many levels; 0 for all
    #[arg(long)]
    depth: Option<u32>,
    /// Show at most this many levels; 0 for all
    #[arg(long)]
    limit: Option<i64>,
}

#[derive(Args)]
struct FindArgs {
    #[command(flatten)]
    common: Common,
    /// A glob (`*`, `?`) against a bare name, not a full path
    pattern: String,
    /// Search instances rather than nets
    #[arg(long)]
    instances: bool,
    /// Search definitions rather than nets
    #[arg(long)]
    modules: bool,
    /// Show at most this many hits; 0 for all
    #[arg(long)]
    limit: Option<i64>,
}

/// What every command needs to reach the design.
#[derive(Args)]
struct Common {
    /// Path to the design database
    db: PathBuf,
    /// Name the top to resolve against, where the design has several
    #[arg(long)]
    top: Option<String>,
    /// Emit the JSON envelope rather than a terminal view
    #[arg(long)]
    json: bool,
}

/// Where this design sits in the coordinates the caller's paths are written
/// in, for the commands that take a path.
///
/// Worked out from the path where it is not given, so it is an override and
/// not a precondition — see `resolve::below_the_anchor`.
#[derive(Args, Clone)]
struct AnchorArg {
    /// Where the design's root sits in your paths, e.g. tb.u_dut (inferred if omitted)
    #[arg(long)]
    anchor: Option<String>,
}

/// How far a walk goes, and what ends it.
#[derive(Args, Clone)]
struct WalkArgs {
    /// Stop after this many hops; 0 for as far as the design goes (default 4)
    #[arg(long)]
    depth: Option<u32>,
    /// Stop at state elements: the answer is then this cycle's logic
    #[arg(long)]
    comb: bool,
    /// Under --comb, cross a latch anyway — its transparent window is the point
    #[arg(long)]
    through_latch: bool,
    /// Leave out the conditions gating a statement entirely
    #[arg(long)]
    no_ctl: bool,
    /// Follow a gating condition transitively, like any other dependency
    #[arg(long, conflicts_with = "no_ctl")]
    follow_ctl: bool,
}

impl WalkArgs {
    fn bounds(&self, max_nodes: Option<usize>) -> cone::Bounds {
        cone::Bounds {
            // Zero is unbounded, as it is for `--limit`: one spelling for
            // "no bound" across the tool. A combinational walk is bounded by
            // the state elements it stops at, so it needs no default.
            max_depth: match self.depth {
                Some(0) => None,
                Some(n) => Some(n),
                None if self.comb => None,
                None => Some(4),
            },
            comb: self.comb,
            through_latch: self.through_latch,
            // One hop by default: a gate says what decided this assignment,
            // and where the gate itself came from is a question about the
            // gate. Followed transitively it stops being about the signal
            // asked for at all.
            gating: match (self.no_ctl, self.follow_ctl) {
                (true, _) => cone::Gating::None,
                (_, true) => cone::Gating::Full,
                _ => cone::Gating::Direct,
            },
            max_nodes,
        }
    }
}

#[derive(Args)]
struct ConeArgs {
    #[command(flatten)]
    common: Common,
    #[command(flatten)]
    at: AnchorArg,
    /// Hierarchical path of the signal, with an optional bit-select
    signal: String,
    #[command(flatten)]
    walk: WalkArgs,
    /// Show at most this many edges; 0 for all
    #[arg(long)]
    limit: Option<i64>,
}

#[derive(Args)]
struct PathArgs {
    #[command(flatten)]
    common: Common,
    #[command(flatten)]
    at: AnchorArg,
    /// Where the route starts
    from: String,
    /// Where it should arrive
    to: String,
    #[command(flatten)]
    walk: WalkArgs,
}

#[derive(Args)]
struct TraceArgs {
    /// Path to the design database
    db: PathBuf,
    /// Hierarchical path of the signal, with an optional bit-select
    signal: String,
    /// Report what reads the signal instead of what drives it
    #[arg(long)]
    load: bool,
    /// Include the conditions that gate a statement as arcs of their own
    #[arg(long)]
    ctl: bool,
    #[command(flatten)]
    at: AnchorArg,
    /// Name the top to resolve against, where the design has several
    #[arg(long)]
    top: Option<String>,

    /// Emit the JSON envelope rather than a terminal view
    #[arg(long)]
    json: bool,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    // Before any command runs, like the arguments themselves: a budget that is
    // not a number is a mistake in the invocation, and a twelve-second walk is
    // the worst moment to find that out.
    let max_nodes = match cone::max_nodes() {
        Ok(bound) => bound,
        Err(message) => {
            eprintln!("error: {message}");
            return ExitCode::from(2);
        }
    };
    let rendered = match cli.command {
        Command::Info { db, limit, json } => {
            let (outcome, diagnostics) = info::run(&db, cone_result::resolve_limit(limit));
            envelope::render(
                "info",
                json!({ "db": db.display().to_string(), "limit": limit }),
                &outcome,
                &diagnostics,
                json,
            )
        }
        Command::Fanin(args) => cone_command(args, Direction::Driver, max_nodes),
        Command::Fanout(args) => cone_command(args, Direction::Load, max_nodes),
        Command::Path(args) => path_command(args, max_nodes),
        Command::Tree(args) => {
            let echo = json!({
                "db": args.common.db.display().to_string(),
                "scope": args.scope,
                "depth": args.depth,
                "limit": args.limit,
                "top": args.common.top,
                "anchor": args.at.anchor,
            });
            let outcome = tree_command(&args);
            envelope::render("tree", echo, &outcome, &[], args.common.json)
        }
        Command::Find(args) => {
            let echo = json!({
                "db": args.common.db.display().to_string(),
                "pattern": args.pattern,
                "instances": args.instances,
                "modules": args.modules,
                "limit": args.limit,
                "top": args.common.top,
            });
            let outcome = find_command(&args);
            let notes = match &outcome {
                Ok(found) => found.notes(),
                Err(_) => Vec::new(),
            };
            envelope::render("find", echo, &outcome, &notes, args.common.json)
        }
        Command::Trace(args) => {
            let echo = json!({
                "db": args.db.display().to_string(),
                "signal": args.signal,
                "load": args.load,
                "ctl": args.ctl,
                "top": args.top,
                "anchor": args.at.anchor,
            });
            let (outcome, notes, at) = trace_command(&args);
            envelope::render_anchored("trace", echo, &outcome, &notes, at.as_ref(), args.json)
        }
    };
    write_out(rendered)
}

type Traced = (Result<trace::Trace, CommandError>, Vec<Diagnostic>, Option<envelope::AnchorNote>);

fn trace_command(args: &TraceArgs) -> Traced {
    match traced(args) {
        Ok((trace, notes, at)) => (Ok(trace), notes, Some(at)),
        Err(e) => (Err(e), Vec::new(), None),
    }
}

fn traced(
    args: &TraceArgs,
) -> Result<(trace::Trace, Vec<Diagnostic>, envelope::AnchorNote), CommandError> {
    let db = open(&args.db)?;
    // What the export could not reach is not visible in an answer's shape: a
    // signal whose driving procedure was skipped reads as undriven.
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&db, &seal))
        .unwrap_or_default();
    let anchor = resolve::anchor(db.conn(), args.top.as_deref(), args.at.anchor.as_deref())
        .map_err(|e| CommandError::new(ErrorCode::NoTop, e))?;

    // A bit-select is split off first, and only where the whole spelling is not
    // itself a name: a signal may legitimately be called `mem[0]` in a path.
    let (path, select) = split_signal(&db, &anchor, &args.signal)?;
    let signal = match resolve::resolve(db.conn(), &anchor, path)
        .map_err(|e| CommandError::new(ErrorCode::SignalNotFound, e))?
    {
        Ok(found) => found,
        Err(u) => {
            let mut prefix = vec![anchor.root_name.clone()];
            prefix.extend(u.valid_prefix.iter().cloned());
            return Err(not_found(
                ErrorCode::SignalNotFound,
                &args.signal,
                &prefix.join("."),
                &u.failing_segment,
                &u.candidates,
                u.anchored_elsewhere,
            ));
        }
    };

    // The declared range is what turns `[7:4]` into offsets. Without one there
    // is nothing to measure the select against.
    let (offsets, spelled) = match select {
        Some(sel) => {
            // An aggregate has no one declared range for offsets to be mapped
            // onto, so there is nothing to measure the select against. Saying
            // so is the alternative to measuring it against a part.
            let decl = signal
                .net
                .data_type
                .as_deref()
                .and_then(|t| bits::declared_range(t, signal.net.width))
                .ok_or_else(|| {
                    CommandError::new(
                        ErrorCode::BadSelect,
                        format!(
                            "{} is {} and has no single declared bit range to select from; \
                             trace the whole object",
                            signal.local,
                            signal.net.data_type.as_deref().unwrap_or("untyped")
                        ),
                    )
                })?;
            let offsets = bits::offsets_of(sel, decl)
                .map_err(|e| CommandError::new(ErrorCode::BadSelect, e))?;
            let spelled = if sel.0 == sel.1 {
                format!("[{}]", sel.0)
            } else {
                format!("[{}:{}]", sel.0, sel.1)
            };
            (Some(offsets), Some(spelled))
        }
        None => (None, None),
    };

    let dir = if args.load { Direction::Load } else { Direction::Driver };
    let trace = trace::run(&db, &anchor, &signal, dir, offsets, spelled, args.ctl)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;
    let at = anchor_note(&anchor, [signal.discarded.as_deref(), None]);
    Ok((trace, notes, at))
}

/// Split a trailing bit-select off the signal path.
///
/// The whole spelling is tried as a name first: a generate element or an
/// unpacked array member is written with brackets and is a name, not a select,
/// and resolving it as one would answer about the wrong object.
fn split_signal<'a>(
    db: &Db,
    anchor: &resolve::Anchor,
    signal: &'a str,
) -> Result<(&'a str, bits::Select), CommandError> {
    let whole = resolve::resolve(db.conn(), anchor, signal)
        .map_err(|e| CommandError::new(ErrorCode::SignalNotFound, e))?;
    if whole.is_ok() {
        return Ok((signal, None));
    }
    let (name, select) = bits::split_select(signal);
    Ok((name, select))
}

fn open(path: &Path) -> Result<Db, CommandError> {
    Db::open(path).map_err(|e| {
        let code = match e {
            designdb::OpenError::NotFound { .. } => ErrorCode::InputNotFound,
            _ => ErrorCode::DbUnreadable,
        };
        CommandError::new(code, e.to_string())
            .with_details(json!({ "path": path.display().to_string() }))
    })
}

/// How many names an error offers. A level of a large design has thousands,
/// and an agent that mistyped one should not be handed the whole design to
/// read back.
const SHOWN: usize = 20;

/// A name that did not resolve, with what is at the level it stopped in.
///
/// The details are the correction: the next call is a fix rather than a search,
/// which is the difference between an agent recovering and an agent guessing.
/// Every command spells them the same way — `valid_prefix` a path, `available`
/// what is at it — or a caller would need one parser per error code.
fn not_found(
    code: ErrorCode,
    asked: &str,
    prefix: &str,
    missing: &str,
    here: &[String],
    elsewhere: bool,
) -> CommandError {
    let close = resolve::close_matches(missing, here, 5);
    // No level of this path named anything here, so it is not written in these
    // coordinates at all. The failing level is then not a name to correct but
    // a level to drop, and offering a spelling correction would send the
    // caller to fix the wrong word.
    let outside = elsewhere;
    let hint = match (outside, close.is_empty()) {
        (true, _) => format!(
            "; nothing above it named this design either — if '{asked}' is anchored at a \
             testbench, name where the root sits with --anchor"
        ),
        (false, false) => format!("; did you mean: {}", close.join(", ")),
        (false, true) => String::new(),
    };
    let what = match code {
        ErrorCode::ScopeNotFound => "a scope",
        _ => "a signal",
    };
    CommandError::new(
        code,
        format!("'{asked}' does not name {what}: '{missing}' is not there{hint}"),
    )
    .with_details(json!({
        "asked": asked,
        "valid_prefix": prefix,
        "failing_segment": missing,
        "close_matches": close,
        "anchored_elsewhere": outside,
        "available": here.iter().take(SHOWN).collect::<Vec<_>>(),
        "available_truncated": here.len() > SHOWN,
    }))
}

fn write_out(rendered: Rendered) -> ExitCode {
    if write_all(&mut std::io::stdout(), &rendered.stdout).is_err() {
        // The reader went away: `| head`, an agent that stopped reading. There
        // is no one left to tell, and the `print!` macros would panic here —
        // printing a Rust backtrace to stderr, which is the one thing the
        // envelope promises never to do.
        return ExitCode::from(EXIT_SIGPIPE);
    }
    let _ = write_all(&mut std::io::stderr(), &rendered.stderr);
    ExitCode::from(rendered.exit_code as u8)
}

/// 128 + SIGPIPE, the shell's convention for a process a closed pipe ended.
const EXIT_SIGPIPE: u8 = 141;

fn write_all(w: &mut impl Write, s: &str) -> std::io::Result<()> {
    w.write_all(s.as_bytes())?;
    w.flush()
}

/// Open the database and choose the root, which every command does first.
fn design(common: &Common, at: &AnchorArg) -> Result<(Db, resolve::Anchor), CommandError> {
    let db = open(&common.db)?;
    let anchor = resolve::anchor(db.conn(), common.top.as_deref(), at.anchor.as_deref())
        .map_err(|e| CommandError::new(ErrorCode::NoTop, e))?;
    Ok((db, anchor))
}

/// The root every path in this answer is spelled against, plus what any of
/// them lost getting there.
fn anchor_note(anchor: &resolve::Anchor, of: [Option<&str>; 2]) -> envelope::AnchorNote {
    let mut discarded: Vec<String> =
        of.into_iter().flatten().map(str::to_string).collect();
    discarded.sort();
    discarded.dedup();
    envelope::AnchorNote { root: anchor.root_name.clone(), discarded }
}

/// A walk that stopped, as the envelope reports it.
///
/// The budget comes from the environment and so is absent from `command.args`;
/// an answer that a budget changed has to carry the number that changed it, or
/// the same command on two machines differs with nothing to say why. A walk
/// that finished never mentions it: there the number changed nothing.
fn walk_failed(e: cone::WalkError) -> CommandError {
    match e {
        cone::WalkError::Db(message) => CommandError::new(ErrorCode::BadDb, message),
        cone::WalkError::TooLarge { max_nodes, nodes, depth, depth_nodes, depth_edges } => {
            // `--depth 0` is unbounded, so a walk that got nowhere must not be
            // told to ask for depth zero — that is the question it just failed.
            let reached = match depth {
                0 => format!(
                    "the bound of {max_nodes} net(s) is smaller than this cone's first hop"
                ),
                d => format!(
                    "depth {d} is the deepest it finished, at {depth_nodes} net(s) \
                     and {depth_edges} arc(s)"
                ),
            };
            let mut ideas = Vec::new();
            if depth > 0 {
                ideas.push(format!("--depth {depth}"));
            }
            ideas.extend([
                "--comb".to_string(),
                "--no-ctl".to_string(),
                "RTLSCANNER_MAX_NODES=<bigger>, or 0 for no bound and the risk is yours"
                    .to_string(),
            ]);
            CommandError::new(
                ErrorCode::BudgetExceeded,
                format!(
                    "this cone reached the walk's bound of {max_nodes} net(s) and was still \
                     growing; {reached}"
                ),
            )
            .with_details(json!({
                "max_nodes": max_nodes,
                "nodes_reached": nodes,
                "last_complete_depth": depth,
                "nodes_at_that_depth": depth_nodes,
                "edges_at_that_depth": depth_edges,
                "try": ideas,
            }))
        }
    }
}

/// Resolve one signal path, with the bit-select it may carry.
fn signal_of(
    db: &Db,
    anchor: &resolve::Anchor,
    spelled: &str,
) -> Result<(resolve::ResolvedSignal, Option<(u64, u64)>), CommandError> {
    let (path, select) = split_signal(db, anchor, spelled)?;
    let signal = match resolve::resolve(db.conn(), anchor, path)
        .map_err(|e| CommandError::new(ErrorCode::SignalNotFound, e))?
    {
        Ok(found) => found,
        Err(u) => {
            let mut prefix = vec![anchor.root_name.clone()];
            prefix.extend(u.valid_prefix.iter().cloned());
            return Err(not_found(
                ErrorCode::SignalNotFound,
                spelled,
                &prefix.join("."),
                &u.failing_segment,
                &u.candidates,
                u.anchored_elsewhere,
            ));
        }
    };
    let window = match select {
        None => None,
        Some(sel) => {
            let decl = signal
                .net
                .data_type
                .as_deref()
                .and_then(|t| bits::declared_range(t, signal.net.width))
                .ok_or_else(|| {
                    CommandError::new(
                        ErrorCode::BadSelect,
                        format!(
                            "{} is {} and has no single declared bit range to select from; \
                             trace the whole object",
                            signal.local,
                            signal.net.data_type.as_deref().unwrap_or("untyped")
                        ),
                    )
                })?;
            Some(
                bits::offsets_of(sel, decl)
                    .map_err(|e| CommandError::new(ErrorCode::BadSelect, e))?,
            )
        }
    };
    Ok((signal, window))
}

fn cone_command(args: ConeArgs, dir: Direction, max_nodes: Option<usize>) -> Rendered {
    let name = match dir {
        Direction::Driver => "fanin",
        Direction::Load => "fanout",
    };
    let echo = json!({
        "db": args.common.db.display().to_string(),
        "signal": args.signal,
        "top": args.common.top,
        "anchor": args.at.anchor,
        "depth": args.walk.depth,
        "comb": args.walk.comb,
        "through_latch": args.walk.through_latch,
        "no_ctl": args.walk.no_ctl,
        "follow_ctl": args.walk.follow_ctl,
        "limit": args.limit,
    });
    let (outcome, notes, at) = match walked(&args, dir, max_nodes) {
        Ok((result, notes, at)) => (Ok(result), notes, Some(at)),
        Err(e) => (Err(e), Vec::new(), None),
    };
    envelope::render_anchored(name, echo, &outcome, &notes, at.as_ref(), args.common.json)
}

fn walked(
    args: &ConeArgs,
    dir: Direction,
    max_nodes: Option<usize>,
) -> Result<(cone_result::ConeResult, Vec<Diagnostic>, envelope::AnchorNote), CommandError> {
    let (db, anchor) = design(&args.common, &args.at)?;
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&db, &seal))
        .unwrap_or_default();
    let (signal, window) = signal_of(&db, &anchor, &args.signal)?;

    let cone = cone::walk(&db, &anchor, &signal, dir, window, args.walk.bounds(max_nodes))
        .map_err(walk_failed)?;
    let limit = cone_result::resolve_limit(args.limit);
    let at = anchor_note(&anchor, [signal.discarded.as_deref(), None]);
    Ok((cone_result::ConeResult::new(cone, limit), notes, at))
}

fn path_command(args: PathArgs, max_nodes: Option<usize>) -> Rendered {
    let echo = json!({
        "db": args.common.db.display().to_string(),
        "from": args.from,
        "to": args.to,
        "top": args.common.top,
        "anchor": args.at.anchor,
        "depth": args.walk.depth,
        "comb": args.walk.comb,
        "through_latch": args.walk.through_latch,
        "no_ctl": args.walk.no_ctl,
        "follow_ctl": args.walk.follow_ctl,
    });
    let (outcome, notes, at) = match routed(&args, max_nodes) {
        Ok((result, notes, at)) => (Ok(result), notes, Some(at)),
        Err(e) => (Err(e), Vec::new(), None),
    };
    envelope::render_anchored("path", echo, &outcome, &notes, at.as_ref(), args.common.json)
}

fn routed(
    args: &PathArgs,
    max_nodes: Option<usize>,
) -> Result<(cone_result::PathResult, Vec<Diagnostic>, envelope::AnchorNote), CommandError> {
    let (db, anchor) = design(&args.common, &args.at)?;
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&db, &seal))
        .unwrap_or_default();
    let (from, _) = signal_of(&db, &anchor, &args.from)?;
    let (to, _) = signal_of(&db, &anchor, &args.to)?;

    // A route search is bounded by finding the route, not by a hop count: a
    // default depth here would report "no path" for one that is simply longer
    // than the default, which is the answer a caller is least able to check.
    let bounds = cone::Bounds {
        max_depth: args.walk.depth.filter(|d| *d > 0),
        ..args.walk.bounds(max_nodes)
    };
    // A walk that gave up is not a walk that found nothing: `found: false`
    // rests on having looked everywhere, so a budget that stopped the search
    // is an error and never an answer.
    let route = cone::find_path(&db, &anchor, &from, &to, bounds).map_err(walk_failed)?;

    // Every net a route names, so the answer spells paths and not ids.
    let mut names = std::collections::HashMap::new();
    if let Some(route) = &route {
        for edge in route {
            for net in [edge.source, edge.target] {
                if let std::collections::hash_map::Entry::Vacant(slot) = names.entry(net)
                    && let Ok(Some(row)) = designdb::schema::net_of(db.conn(), net)
                    && let Ok(scope) = trace::instance_path(db.conn(), &anchor, row.inst_id, '.')
                {
                    slot.insert(format!("{scope}.{}", row.net_name));
                }
            }
        }
    }
    let at = anchor_note(&anchor, [from.discarded.as_deref(), to.discarded.as_deref()]);
    Ok((
        cone_result::PathResult {
            from: from.path(&anchor.root_name, '.'),
            to: to.path(&anchor.root_name, '.'),
            bounds,
            route,
            names,
        },
        notes,
        at,
    ))
}

fn tree_command(args: &TreeArgs) -> Result<browse::Tree, CommandError> {
    let (db, anchor) = design(&args.common, &args.at)?;
    // A scope names a level of the tree, not a net: the walk down is the same
    // one, and what it stops at is a node rather than a name inside one.
    let (node, path) = match &args.scope {
        None => (anchor.root, anchor.root_name.clone()),
        Some(scope) => scope_node(&db, &anchor, scope)?,
    };
    // Zero is unbounded, as it is everywhere else in this tool.
    let depth = match args.depth {
        Some(0) => None,
        other => other.or(Some(3)),
    };
    browse::tree(&db, node, &path, depth, args.limit)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))
}

/// Resolve a path to the tree level it names.
fn scope_node(
    db: &Db,
    anchor: &resolve::Anchor,
    scope: &str,
) -> Result<(i64, String), CommandError> {
    let (levels, _discarded, _reached) = resolve::below_the_anchor(db.conn(), anchor, scope)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;

    let mut at = anchor.root;
    let mut walked = vec![anchor.root_name.clone()];
    for level in levels {
        let found = schema::child_node(db.conn(), at, &level)
            .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;
        match found {
            Some(child) => {
                at = child.node_id;
                walked.push(child.node_name);
            }
            None => {
                // What is at the level that did resolve, so the next call is a
                // correction rather than a search.
                let here: Vec<String> = schema::children_of(db.conn(), at)
                    .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?
                    .into_iter()
                    .map(|n| n.node_name)
                    .collect();
                return Err(not_found(
                    ErrorCode::ScopeNotFound,
                    scope,
                    &walked.join("."),
                    &level,
                    &here,
                    // The scope walk starts below the anchor, so anything that
                    // fails here failed inside the design.
                    false,
                ));
            }
        }
    }
    Ok((at, walked.join(".")))
}

fn find_command(args: &FindArgs) -> Result<browse::Found, CommandError> {
    // `find` matches a name, never a path, so there is no anchor to state.
    let (db, anchor) = design(&args.common, &AnchorArg { anchor: None })?;
    let kind = match (args.instances, args.modules) {
        (true, true) => {
            return Err(CommandError::new(
                ErrorCode::BadSelect,
                "--instances and --modules ask for different things; pick one",
            ));
        }
        (true, false) => browse::Kind::Instance,
        (false, true) => browse::Kind::Module,
        (false, false) => browse::Kind::Net,
    };
    browse::find(&db, &anchor, &args.pattern, kind, args.limit)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))
}
