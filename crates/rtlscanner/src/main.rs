//! RTLScanner — signal trace, driver and load analysis over a design database.
//!
//! The database is `rtl-designdb`'s: the RTL is elaborated once, into rows this
//! tool queries. Structure comes from those rows and from nowhere else — this
//! tool reads no RTL and no waveform, so what it reports is what the export
//! recorded, at the precision the export recorded it.

mod envelope;
mod info;

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use serde_json::json;

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
    };

    print!("{}", rendered.stdout);
    let _ = std::io::stdout().flush();
    eprint!("{}", rendered.stderr);
    ExitCode::from(rendered.exit_code as u8)
}
