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
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use designdb::{Db, Direction, bits, resolve, schema};
use serde_json::json;

use envelope::{CommandError, Diagnostic, ErrorCode, Rendered};

#[derive(Parser)]
#[command(name = "rtlscanner", version, about, long_about = None)]
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
    /// A glob against the name — `*` and `?` as a shell spells them. A net's
    /// name is the one relative to its instance, not its full path
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
    /// Strip this prefix from paths — a testbench scope the design has never
    /// heard of, as a waveform tool spells it
    #[arg(long)]
    strip_prefix: Option<String>,
    /// Emit the JSON envelope rather than a terminal view
    #[arg(long)]
    json: bool,
}

/// How far a walk goes, and what ends it.
#[derive(Args, Clone)]
struct WalkArgs {
    /// Stop after this many hops; 0 for as far as the design goes. Unbounded
    /// by default in `--comb`, where state elements bound it instead
    #[arg(long)]
    depth: Option<u32>,
    /// Stop at state elements: the answer is then this cycle's logic
    #[arg(long)]
    comb: bool,
    /// Cross a latch anyway — for a glitch, a loop closing through one, or a
    /// pulse-latch borrow, where its transparent window is the point
    #[arg(long)]
    through_latch: bool,
    /// Leave out the conditions that gate a statement. They are real
    /// dependencies and usually the numerous ones
    #[arg(long)]
    no_control: bool,
}

impl WalkArgs {
    fn bounds(&self) -> cone::Bounds {
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
            control: !self.no_control,
        }
    }
}

#[derive(Args)]
struct ConeArgs {
    #[command(flatten)]
    common: Common,
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
    control: bool,
    /// Name the top to resolve against, where the design has several
    #[arg(long)]
    top: Option<String>,
    /// Strip this prefix from the path — a testbench scope the design has
    /// never heard of, as a waveform tool spells it
    #[arg(long)]
    strip_prefix: Option<String>,
    /// Emit the JSON envelope rather than a terminal view
    #[arg(long)]
    json: bool,
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let rendered = match cli.command {
        Command::Info { db, json } => {
            let (outcome, diagnostics) = info::run(&db);
            envelope::render(
                "info",
                json!({ "db": db.display().to_string() }),
                &outcome,
                &diagnostics,
                json,
            )
        }
        Command::Fanin(args) => cone_command(args, Direction::Driver),
        Command::Fanout(args) => cone_command(args, Direction::Load),
        Command::Path(args) => path_command(args),
        Command::Tree(args) => {
            let echo = json!({
                "db": args.common.db.display().to_string(),
                "scope": args.scope,
                "depth": args.depth,
                "limit": args.limit,
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
            });
            let outcome = find_command(&args);
            envelope::render("find", echo, &outcome, &[], args.common.json)
        }
        Command::Trace(args) => {
            let echo = json!({
                "db": args.db.display().to_string(),
                "signal": args.signal,
                "load": args.load,
                "control": args.control,
                "top": args.top,
                "strip_prefix": args.strip_prefix,
            });
            let (outcome, notes) = trace_command(&args);
            envelope::render("trace", echo, &outcome, &notes, args.json)
        }
    };
    write_out(rendered)
}

fn trace_command(args: &TraceArgs) -> (Result<trace::Trace, CommandError>, Vec<Diagnostic>) {
    match traced(args) {
        Ok((trace, notes)) => (Ok(trace), notes),
        Err(e) => (Err(e), Vec::new()),
    }
}

fn traced(args: &TraceArgs) -> Result<(trace::Trace, Vec<Diagnostic>), CommandError> {
    let db = open(&args.db)?;
    // What the export could not reach is not visible in an answer's shape: a
    // signal whose driving procedure was skipped reads as undriven.
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&seal))
        .unwrap_or_default();
    let anchor = resolve::anchor(db.conn(), args.top.as_deref(), args.strip_prefix.as_deref())
        .map_err(|e| CommandError::new(ErrorCode::NoTop, e))?;

    // A bit-select is split off first, and only where the whole spelling is not
    // itself a name: a signal may legitimately be called `mem[0]` in a path.
    let (path, select) = split_signal(&db, &anchor, &args.signal)?;
    let signal = match resolve::resolve(db.conn(), &anchor, path)
        .map_err(|e| CommandError::new(ErrorCode::SignalNotFound, e))?
    {
        Ok(found) => found,
        Err(u) => return Err(not_found(&args.signal, &u)),
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
    let trace = trace::run(&db, &anchor, &signal, dir, offsets, spelled, args.control)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;
    Ok((trace, notes))
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
) -> Result<(&'a str, Option<(i64, i64)>), CommandError> {
    let whole = resolve::resolve(db.conn(), anchor, signal)
        .map_err(|e| CommandError::new(ErrorCode::SignalNotFound, e))?;
    if whole.is_ok() {
        return Ok((signal, None));
    }
    let (name, select) = bits::split_select(signal);
    Ok((name, select))
}

fn open(path: &PathBuf) -> Result<Db, CommandError> {
    Db::open(path).map_err(|e| {
        let code = match e {
            designdb::OpenError::NotFound { .. } => ErrorCode::InputNotFound,
            _ => ErrorCode::DbUnreadable,
        };
        CommandError::new(code, e.to_string())
            .with_details(json!({ "path": path.display().to_string() }))
    })
}

/// A name that did not resolve, with what is at the level it stopped in.
///
/// The details are the correction: the next call is a fix rather than a search,
/// which is the difference between an agent recovering and an agent guessing.
fn not_found(asked: &str, u: &resolve::Unresolved) -> CommandError {
    const SHOWN: usize = 20;
    let close = resolve::close_matches(&u.failing_segment, &u.candidates, 5);
    let hint = if close.is_empty() {
        String::new()
    } else {
        format!("; did you mean: {}", close.join(", "))
    };
    CommandError::new(
        ErrorCode::SignalNotFound,
        format!("'{asked}' does not name a signal: '{}' is not there{hint}", u.failing_segment),
    )
    .with_details(json!({
        "signal": asked,
        "valid_prefix": u.valid_prefix,
        "failing_segment": u.failing_segment,
        "close_matches": close,
        "available": u.candidates.iter().take(SHOWN).collect::<Vec<_>>(),
        "available_truncated": u.candidates.len() > SHOWN,
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
fn design(common: &Common) -> Result<(Db, resolve::Anchor), CommandError> {
    let db = open(&common.db)?;
    let anchor = resolve::anchor(db.conn(), common.top.as_deref(), common.strip_prefix.as_deref())
        .map_err(|e| CommandError::new(ErrorCode::NoTop, e))?;
    Ok((db, anchor))
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
        Err(u) => return Err(not_found(spelled, &u)),
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

fn cone_command(args: ConeArgs, dir: Direction) -> Rendered {
    let name = match dir {
        Direction::Driver => "fanin",
        Direction::Load => "fanout",
    };
    let echo = json!({
        "db": args.common.db.display().to_string(),
        "signal": args.signal,
        "depth": args.walk.depth,
        "comb": args.walk.comb,
        "through_latch": args.walk.through_latch,
        "no_control": args.walk.no_control,
        "limit": args.limit,
    });
    let (outcome, notes) = match walked(&args, dir) {
        Ok((result, notes)) => (Ok(result), notes),
        Err(e) => (Err(e), Vec::new()),
    };
    envelope::render(name, echo, &outcome, &notes, args.common.json)
}

fn walked(
    args: &ConeArgs,
    dir: Direction,
) -> Result<(cone_result::ConeResult, Vec<Diagnostic>), CommandError> {
    let (db, anchor) = design(&args.common)?;
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&seal))
        .unwrap_or_default();
    let (signal, window) = signal_of(&db, &anchor, &args.signal)?;

    let cone = cone::walk(&db, &anchor, &signal, dir, window, args.walk.bounds())
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;
    let limit = cone_result::resolve_limit(args.limit);
    Ok((cone_result::ConeResult::new(cone, limit), notes))
}

fn path_command(args: PathArgs) -> Rendered {
    let echo = json!({
        "db": args.common.db.display().to_string(),
        "from": args.from,
        "to": args.to,
        "depth": args.walk.depth,
        "comb": args.walk.comb,
        "through_latch": args.walk.through_latch,
        "no_control": args.walk.no_control,
    });
    let (outcome, notes) = match routed(&args) {
        Ok((result, notes)) => (Ok(result), notes),
        Err(e) => (Err(e), Vec::new()),
    };
    envelope::render("path", echo, &outcome, &notes, args.common.json)
}

fn routed(args: &PathArgs) -> Result<(cone_result::PathResult, Vec<Diagnostic>), CommandError> {
    let (db, anchor) = design(&args.common)?;
    let notes = designdb::schema::db_info(db.conn())
        .map(|seal| info::trust_notes(&seal))
        .unwrap_or_default();
    let (from, _) = signal_of(&db, &anchor, &args.from)?;
    let (to, _) = signal_of(&db, &anchor, &args.to)?;

    // A route search is bounded by finding the route, not by a hop count: a
    // default depth here would report "no path" for one that is simply longer
    // than the default, which is the answer a caller is least able to check.
    let bounds = cone::Bounds { max_depth: args.walk.depth.filter(|d| *d > 0), ..args.walk.bounds() };
    let route = cone::find_path(&db, &anchor, &from, &to, bounds)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))?;

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
    Ok((
        cone_result::PathResult {
            from: from.path(&anchor.root_name, '.'),
            to: to.path(&anchor.root_name, '.'),
            bounds,
            route,
            names,
        },
        notes,
    ))
}

fn tree_command(args: &TreeArgs) -> Result<browse::Tree, CommandError> {
    let (db, anchor) = design(&args.common)?;
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
    let stripped = resolve::strip_prefix(anchor, scope)
        .map_err(|e| CommandError::new(ErrorCode::ScopeNotFound, e))?;
    let mut segments = resolve::segments(stripped);
    if segments.first() == Some(&anchor.root_name.as_str()) {
        segments.remove(0);
    }

    let mut at = anchor.root;
    let mut walked = vec![anchor.root_name.clone()];
    for segment in segments {
        let found = schema::child_node(db.conn(), at, segment)
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
                let close = resolve::close_matches(segment, &here, 5);
                return Err(CommandError::new(
                    ErrorCode::ScopeNotFound,
                    format!("'{scope}' does not name a scope: '{segment}' is not there"),
                )
                .with_details(json!({
                    "scope": scope,
                    "valid_prefix": walked.join("."),
                    "failing_segment": segment,
                    "close_matches": close,
                    "children": here,
                })));
            }
        }
    }
    Ok((at, walked.join(".")))
}

fn find_command(args: &FindArgs) -> Result<browse::Found, CommandError> {
    let (db, anchor) = design(&args.common)?;
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
