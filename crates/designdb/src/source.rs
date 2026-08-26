//! Reading a statement's text back out of the RTL.
//!
//! The database records where a statement is, not what it says. Reading the
//! line is worth doing — it is the answer a person wants — but only while the
//! file still is what was exported: a digest that no longer matches means the
//! line numbers point into a file that has moved on, and quoting from it would
//! attribute someone else's code to this statement.

use std::collections::HashMap;

use crate::{digest, schema};

/// Whether a file still is what the export read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceState {
    /// Byte for byte what was exported: a location in it means what it says.
    Read,
    /// Present but changed. Line numbers name lines that are no longer these.
    Stale,
    /// Not where the export read it.
    Missing,
}

impl SourceState {
    pub fn tag(self) -> &'static str {
        match self {
            SourceState::Read => "read",
            SourceState::Stale => "stale",
            SourceState::Missing => "missing",
        }
    }
}

/// Longest line worth putting in a table: a generated file's single line can
/// run to kilobytes, and the point of quoting a statement is to read it.
const MAX_LEN: usize = 200;

/// Source files, checked once and read once each.
#[derive(Default)]
pub struct SourceCache {
    checked: HashMap<String, SourceState>,
    lines: HashMap<String, Vec<String>>,
}

impl SourceCache {
    pub fn new() -> SourceCache {
        SourceCache::default()
    }

    /// The text at `file_path:line`, and whether it can be trusted.
    ///
    /// A line is returned only in the `Read` state. The other two are the
    /// caller's to report: what the statement was is unknown, which is
    /// different from the statement having no text.
    pub fn line(
        &mut self,
        c: &rusqlite::Connection,
        file_path: &str,
        line: u32,
    ) -> (Option<String>, SourceState) {
        let state = self.state(c, file_path);
        if state != SourceState::Read || line == 0 {
            return (None, state);
        }
        let text = self
            .lines
            .get(file_path)
            .and_then(|lines| lines.get(line as usize - 1))
            .map(|l| l.trim())
            .filter(|l| !l.is_empty() && l.len() <= MAX_LEN)
            .map(str::to_string);
        (text, state)
    }

    /// Whether this file still hashes to what the export recorded, reading it
    /// in if so. One check and one read per file.
    fn state(&mut self, c: &rusqlite::Connection, file_path: &str) -> SourceState {
        if let Some(state) = self.checked.get(file_path) {
            return *state;
        }
        let state = self.check(c, file_path);
        self.checked.insert(file_path.to_string(), state);
        state
    }

    fn check(&mut self, c: &rusqlite::Connection, file_path: &str) -> SourceState {
        // Opened by the path the export read, not by the spelling rows carry:
        // that one is relative to a working directory nothing records.
        let Ok(Some((src_path, recorded))) = schema::source_file(c, file_path) else {
            return SourceState::Missing;
        };
        let Ok(bytes) = std::fs::read(&src_path) else { return SourceState::Missing };

        // The digest is checked against the bytes just read, not against the
        // file as it may be a moment later.
        if recorded != digest::sha256_hex(&bytes) {
            return SourceState::Stale;
        }
        let text = String::from_utf8_lossy(&bytes);
        self.lines.insert(file_path.to_string(), text.lines().map(str::to_string).collect());
        SourceState::Read
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Db;
    use crate::open::fixture::{tmp, write_db};

    fn seeded(dir: &std::path::Path, rtl: &str) -> Db {
        let sv = dir.join("dut.sv");
        std::fs::write(&sv, rtl).unwrap();
        let path = dir.join("design.db");
        write_db(
            &path,
            crate::SCHEMA_VERSION,
            &[&format!(
                "INSERT INTO src_file(id, path, digest) VALUES (1, '{}', '{}');
             INSERT INTO file(id, path, src_file_id) VALUES (1, 'dut.sv', 1);",
                sv.display(),
                digest::sha256_hex(rtl.as_bytes())
            )],
        );
        Db::open(&path).unwrap()
    }

    #[test]
    fn a_line_is_read_back_only_while_the_digest_holds() {
        let dir = tmp("source");
        let rtl = "module dut;\n  assign q = d;\nendmodule\n";
        let db = seeded(&dir, rtl);
        let mut cache = SourceCache::new();

        let (text, state) = cache.line(db.conn(), "dut.sv", 2);
        assert_eq!(text.as_deref(), Some("assign q = d;"));
        assert_eq!(state, SourceState::Read);

        // The file moves on. The row still names line 2; line 2 is no longer
        // the statement it named, so nothing is quoted.
        std::fs::write(dir.join("dut.sv"), format!("// a new first line\n{rtl}")).unwrap();
        let mut cache = SourceCache::new();
        let (text, state) = cache.line(db.conn(), "dut.sv", 2);
        assert_eq!(text, None);
        assert_eq!(state, SourceState::Stale);

        std::fs::remove_file(dir.join("dut.sv")).unwrap();
        let mut cache = SourceCache::new();
        assert_eq!(cache.line(db.conn(), "dut.sv", 2).1, SourceState::Missing);
    }

    #[test]
    fn a_line_that_says_nothing_useful_is_left_out() {
        let dir = tmp("source-empty");
        let long = "x".repeat(MAX_LEN + 1);
        let rtl = format!("module dut;\n\n  assign q = {long};\nendmodule\n");
        let db = seeded(&dir, &rtl);
        let mut cache = SourceCache::new();

        assert_eq!(cache.line(db.conn(), "dut.sv", 2).0, None, "a blank line");
        assert_eq!(cache.line(db.conn(), "dut.sv", 3).0, None, "a line too long to read");
        assert_eq!(cache.line(db.conn(), "dut.sv", 99).0, None, "past the end");
        assert_eq!(cache.line(db.conn(), "dut.sv", 0).0, None, "no line at all");
        // Each of those is still a file that was read: the state is about the
        // file, not about whether one line had something to show.
        assert_eq!(cache.line(db.conn(), "dut.sv", 1).1, SourceState::Read);
    }
}
