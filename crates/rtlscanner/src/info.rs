//! `info` — what this database is, and how far it can be trusted.
//!
//! Every other command answers from rows this one vouches for. The export
//! writes a database and exits 0 even when elaboration errored, so "the file
//! opened" is not the same as "the answers cover the design"; the counts in the
//! seal are what separates them, and a source file that has moved on since the
//! export is what separates a statement's recorded location from the line that
//! now lives there.

use std::path::{Path, PathBuf};

use designdb::{Db, DbInfo, OpenError, digest, schema};
use serde_json::{Value, json};

use crate::envelope::{CommandError, CommandResult, Diagnostic, ErrorCode};

/// Whether a source file still hashes to what the export recorded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceState {
    /// Byte-for-byte what was exported: a location in it means what it says.
    Current,
    /// Present but changed. Line numbers in the database point into a file
    /// that has moved on.
    Stale,
    /// Not where the export read it.
    Missing,
}

impl SourceState {
    fn tag(self) -> &'static str {
        match self {
            SourceState::Current => "current",
            SourceState::Stale => "stale",
            SourceState::Missing => "missing",
        }
    }
}

pub struct Info {
    path: PathBuf,
    seal: DbInfo,
    sources: Vec<(String, SourceState)>,
    /// Hierarchical references the export named and did not resolve, as
    /// (reads, writes). Not in the seal, and not derivable from it.
    unresolved: (i64, i64),
    /// How many unchanged sources to list. Every source that is not current is
    /// listed whatever this says.
    limit: usize,
}

/// What a caller should know before trusting an answer from this database.
///
/// Every command that answers from these rows reports this, not just the one
/// that reports the seal: a `no_driver_found` is read as "nothing drives it",
/// and an export that skipped the procedure doing the driving looks exactly
/// like that. The sources are checked here too — a note about them that only
/// `info` could reach would be a note the commands that quote lines never
/// make.
pub fn trust_notes(db: &Db, seal: &DbInfo) -> Vec<Diagnostic> {
    let info = Info {
        path: PathBuf::new(),
        seal: seal.clone(),
        sources: source_states(db),
        unresolved: unresolved_refs(db),
        limit: 0,
    };
    notes(&info)
}

/// Every source the export recorded, against what is on disk now.
fn source_states(db: &Db) -> Vec<(String, SourceState)> {
    db.source_files()
        .map(|files| files.into_iter().map(|(p, d)| (p.clone(), check_source(&p, &d))).collect())
        .unwrap_or_default()
}

/// References the export wrote as a path and could not resolve to a net, by
/// direction. A read that did not resolve leaves a net with fewer sources than
/// it has; a write that did not resolve leaves a net reading as undriven while
/// something plainly writes it. Neither is in the seal, and both are the
/// difference between `no_driver_found` meaning "nothing does" and meaning
/// "the export could not say what does".
fn unresolved_refs(db: &Db) -> (i64, i64) {
    schema::unresolved_refs(db.conn()).unwrap_or((0, 0))
}

impl Info {
    /// The sources listed in the answer: everything that is not current, and
    /// as many current ones as any other list this tool returns. A design of a
    /// few thousand files would otherwise make the first command an agent runs
    /// the largest answer it ever reads, and the files worth reading are the
    /// ones that moved.
    fn shown_sources(&self) -> Vec<&(String, SourceState)> {
        let (moved, current): (Vec<_>, Vec<_>) =
            self.sources.iter().partition(|(_, s)| *s != SourceState::Current);
        moved.into_iter().chain(current.into_iter().take(self.limit)).collect()
    }

    fn stale(&self) -> usize {
        self.sources.iter().filter(|(_, s)| *s == SourceState::Stale).count()
    }

    fn missing(&self) -> usize {
        self.sources.iter().filter(|(_, s)| *s == SourceState::Missing).count()
    }

    /// The counts that make an export `partial`, in the order the schema
    /// documents them, with the zero ones dropped.
    fn shortfalls(&self) -> Vec<(&'static str, i64)> {
        let s = &self.seal;
        [
            ("errors", s.error_count),
            ("empty procedures", s.empty_procedure_count),
            ("duplicate paths", s.duplicate_path_count),
            ("truncated calls", s.truncated_call_count),
            ("unanalysed instances", s.unanalysed_inst_count),
        ]
        .into_iter()
        .filter(|(_, n)| *n > 0)
        .collect()
    }

    /// Constructs the export declined to model. They do not make it `partial`,
    /// and a trace that ends in one is a boundary rather than a gap: the object
    /// is named, and what is inside it was never going to be here.
    fn declined(&self) -> Vec<(&'static str, i64)> {
        let s = &self.seal;
        [
            ("unresolved instantiations", s.unresolved_count),
            ("checker instantiations", s.checker_inst_count),
        ]
        .into_iter()
        .filter(|(_, n)| *n > 0)
        .collect()
    }
}

impl CommandResult for Info {
    fn to_json(&self) -> (Value, Value) {
        let s = &self.seal;
        let data = json!({
            "path": self.path.display().to_string(),
            "schema_version": s.schema_version,
            "producer": {
                "tool": s.tool,
                "tool_version": s.tool_version,
                "slang_version": s.slang_version,
                "revision": s.producer_revision,
                "config_digest": s.config_digest,
            },
            "top": s.top,
            "analysis": {
                "status": s.analysis_status,
                "error_count": s.error_count,
                "unresolved_count": s.unresolved_count,
                "empty_procedure_count": s.empty_procedure_count,
                "duplicate_path_count": s.duplicate_path_count,
                "recursion_count": s.recursion_count,
                "truncated_call_count": s.truncated_call_count,
                "checker_inst_count": s.checker_inst_count,
                "unanalysed_inst_count": s.unanalysed_inst_count,
                // Not from the seal: the export does not count these, and a
                // consumer reading `no_driver_found` needs them.
                "unresolved_ref_reads": self.unresolved.0,
                "unresolved_ref_writes": self.unresolved.1,
            },
            "sources": self.shown_sources().into_iter().map(|(p, state)| json!({
                "path": p,
                "state": state.tag(),
            })).collect::<Vec<_>>(),
        });
        let summary = json!({
            "schema_version": s.schema_version,
            "analysis_status": s.analysis_status,
            "sources": self.sources.len(),
            "shown_sources": self.shown_sources().len(),
            "sources_stale": self.stale(),
            "sources_missing": self.missing(),
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let s = &self.seal;
        let mut out = String::new();
        out.push_str(&format!("Database: {}\n", self.path.display()));
        out.push_str(&format!(
            "Schema:   v{}  ({} {})\n",
            s.schema_version, s.tool, s.tool_version
        ));
        out.push_str(&format!("Top:      {}\n", s.top.as_deref().unwrap_or("(none recorded)")));
        out.push_str(&format!("Analysis: {}\n", s.analysis_status));

        for (label, n) in self.shortfalls() {
            out.push_str(&format!("  short:    {n} {label}\n"));
        }
        for (label, n) in self.declined() {
            out.push_str(&format!("  declined: {n} {label}\n"));
        }
        if s.recursion_count > 0 {
            out.push_str(&format!("  truncated: {} recursive instance(s)\n", s.recursion_count));
        }

        let (stale, missing) = (self.stale(), self.missing());
        out.push_str(&format!("Sources:  {} file(s)", self.sources.len()));
        if stale > 0 || missing > 0 {
            out.push_str(&format!(", {stale} stale, {missing} missing"));
        }
        out.push('\n');
        for (path, state) in self.sources.iter().filter(|(_, s)| *s != SourceState::Current) {
            out.push_str(&format!("  {}: {path}\n", state.tag()));
        }
        let shown = self.shown_sources().len();
        if shown < self.sources.len() {
            out.push_str(&format!(
                "  ({} of {} listed; --limit 0 for all)\n",
                shown,
                self.sources.len()
            ));
        }
        out
    }
}

/// Read the seal and check every recorded source against what is on disk.
pub fn run(path: &Path, limit: usize) -> (Result<Info, CommandError>, Vec<Diagnostic>) {
    let db = match Db::open(path) {
        Ok(db) => db,
        Err(e) => {
            // The reason travels with the error: a path to fix and a database
            // to re-export are different jobs, and asking the filesystem a
            // second time would only guess at what open already knew.
            let code = match e {
                OpenError::NotFound { .. } => ErrorCode::InputNotFound,
                _ => ErrorCode::DbUnreadable,
            };
            return (
                Err(CommandError::new(code, e.to_string())
                    .with_details(json!({ "path": path.display().to_string() }))),
                Vec::new(),
            );
        }
    };

    let seal = match schema::db_info(db.conn()) {
        Ok(seal) => seal,
        Err(message) => return (Err(CommandError::new(ErrorCode::BadDb, message)), Vec::new()),
    };
    if let Err(message) = db.source_files() {
        return (Err(CommandError::new(ErrorCode::BadDb, message)), Vec::new());
    }

    let info = Info {
        path: path.to_path_buf(),
        seal,
        sources: source_states(&db),
        unresolved: unresolved_refs(&db),
        limit,
    };
    let notes = notes(&info);
    (Ok(info), notes)
}

/// Hash the file where the export read it, and say how it compares.
fn check_source(path: &str, recorded: &str) -> SourceState {
    match std::fs::read(path) {
        Ok(bytes) if digest::sha256_hex(&bytes) == recorded => SourceState::Current,
        Ok(_) => SourceState::Stale,
        Err(_) => SourceState::Missing,
    }
}

/// What a caller should know before trusting an answer from this database.
fn notes(info: &Info) -> Vec<Diagnostic> {
    let mut notes = Vec::new();

    let shortfalls = info.shortfalls();
    match info.seal.analysis_status.as_str() {
        "hierarchy_only" => notes.push(Diagnostic::warning(
            "the compilation errored fatally: this database has hierarchy and no dataflow, \
             so no driver or load query can answer",
        )),
        // The status is never a claim a consumer cannot look at: `partial` is
        // exactly the five counts, so `partial` with none of them is a seal
        // contradicting itself rather than an export with a small shortfall.
        "partial" if shortfalls.is_empty() => notes.push(Diagnostic::warning(
            "the export calls itself partial while every count that causes it is zero; \
             the seal contradicts itself and the file should be re-exported",
        )),
        "partial" => notes.push(Diagnostic::warning(format!(
            "the export is partial ({}); answers may be missing rows rather than reporting none",
            shortfalls.iter().map(|(l, n)| format!("{n} {l}")).collect::<Vec<_>>().join(", ")
        ))),
        "complete" => {}
        other => notes.push(Diagnostic::warning(format!(
            "the export reports an analysis status of '{other}', which is not one of \
             complete, partial or hierarchy_only"
        ))),
    }

    if info.seal.recursion_count > 0 {
        // Not a shortfall and not a decline: the tree holds a prefix. An
        // instance whose module and parameters are already an ancestor's keeps
        // its own rows and no children, so a query below one of these answers
        // nothing without anything being wrong.
        notes.push(Diagnostic::warning(format!(
            "the hierarchy stops at {} recursive instance(s): what is below them was \
             never exported, so a query there is empty rather than undriven",
            info.seal.recursion_count
        )));
    }

    if info.seal.empty_procedure_count > 0 {
        // slang drops a statement it marks bad along with its enclosing block,
        // so a procedure calling a PLI task it does not know contributes none
        // of its drivers. Common enough in a testbench to name outright.
        notes.push(Diagnostic::warning(format!(
            "{} procedure(s) were skipped: every driver they wrote is absent, \
             which reads as an undriven signal rather than an error",
            info.seal.empty_procedure_count
        )));
    }

    // A write the export could not attribute leaves the net it should have
    // driven reading as undriven, which is the one answer a caller cannot
    // check from the outside.
    if info.unresolved.1 > 0 {
        notes.push(Diagnostic::warning(format!(
            "{} hierarchical write(s) were named and not resolved to a net: a signal one of \
             them drives answers no_driver_found rather than naming them",
            info.unresolved.1
        )));
    }

    let (stale, missing) = (info.stale(), info.missing());
    if stale + missing > 0 {
        notes.push(Diagnostic::warning(format!(
            "{stale} source file(s) changed and {missing} missing since the export: \
             recorded locations still name lines, but not these lines"
        )));
    }
    notes
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_source_that_still_hashes_to_the_export_is_current() {
        let dir = std::env::temp_dir().join(format!("rtlscanner-info-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("dut.sv");
        std::fs::write(&file, b"module dut; endmodule\n").unwrap();
        let recorded = digest::sha256_hex(b"module dut; endmodule\n");

        let path = file.to_str().unwrap();
        assert_eq!(check_source(path, &recorded), SourceState::Current);

        std::fs::write(&file, b"module dut; wire w; endmodule\n").unwrap();
        assert_eq!(check_source(path, &recorded), SourceState::Stale);

        std::fs::remove_file(&file).unwrap();
        assert_eq!(check_source(path, &recorded), SourceState::Missing);
    }
}
