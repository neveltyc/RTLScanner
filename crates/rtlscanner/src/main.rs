//! RTLScanner — signal trace, driver and load analysis over a design database.
//!
//! The RTL is elaborated once, by `rtl-designdb`, into rows this tool queries.
//! Structure comes from those rows and from nowhere else — this tool reads no
//! RTL beyond quoting a line it can verify, and no waveform — so what it
//! reports is what the export recorded, at the precision it recorded it.

mod envelope;
mod info;
mod trace;

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Args, Parser, Subcommand};
use designdb::{Db, Direction, bits, resolve};
use serde_json::json;

use envelope::{CommandError, ErrorCode, Rendered};

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
        Command::Trace(args) => {
            let echo = json!({
                "db": args.db.display().to_string(),
                "signal": args.signal,
                "load": args.load,
                "control": args.control,
                "top": args.top,
                "strip_prefix": args.strip_prefix,
            });
            let outcome = trace_command(&args);
            envelope::render("trace", echo, &outcome, &[], args.json)
        }
    };
    write_out(rendered)
}

fn trace_command(args: &TraceArgs) -> Result<trace::Trace, CommandError> {
    let db = open(&args.db)?;
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
            let decl = signal
                .net
                .data_type
                .as_deref()
                .and_then(bits::declared_range)
                .ok_or_else(|| {
                    CommandError::new(
                        ErrorCode::BadSelect,
                        format!(
                            "{} has no declared bit range to select from",
                            signal.local
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
    trace::run(&db, &anchor, &signal, dir, offsets, spelled, args.control)
        .map_err(|e| CommandError::new(ErrorCode::BadDb, e))
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

