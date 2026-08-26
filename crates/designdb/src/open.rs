//! Opening a `design.db`.
//!
//! The file is stock SQLite — rtl-designdb bundles its own copy of the library
//! precisely so anything speaking SQL can read the result. What has to be
//! checked is the schema version: this reader was written against exactly one,
//! and an unrecognised version is refused rather than parsed on the assumption
//! the layout held. Reading a moved column would produce a confident wrong
//! answer, which is worse than no answer.

use std::fmt;
use std::path::{Path, PathBuf};

use rusqlite::Connection;

use crate::err;

/// The rtl-designdb schema this reader was written against. The version is the
/// kit's *consumption contract*, not its DDL: a column added, a value domain
/// moved or a row's meaning changed all bump it, so equality is the only test
/// that holds. A newer file is refused by the kit's own rule — a reader that
/// does not know the version does not get to guess — and an older one because
/// its columns still exist under meanings that have since moved, which fails
/// silently rather than loudly.
pub const SCHEMA_VERSION: i64 = 19;

/// Why a file could not be opened as a design database.
///
/// The variants are distinct facts a caller acts on differently — a path to fix
/// against a database to re-export — so the reason travels with the message
/// rather than being re-derived from the filesystem afterwards.
#[derive(Debug)]
pub enum OpenError {
    /// Nothing readable at that path, with the reason it could not be reached.
    NotFound { path: PathBuf, reason: String },
    /// The bytes are not a SQLite database.
    NotADatabase { path: PathBuf, reason: String },
    /// A database, but with no `meta` version to check.
    NoSchemaVersion { path: PathBuf },
    /// A design database of a version this build does not read.
    VersionMismatch { path: PathBuf, found: i64, tool: Option<String> },
}

impl fmt::Display for OpenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let msg = match self {
            OpenError::NotFound { path, reason } => {
                format!("{}: {reason}.", path.display())
            }
            OpenError::NotADatabase { path, reason } => {
                format!("{} did not open as a database: {reason}", path.display())
            }
            OpenError::NoSchemaVersion { path } => format!(
                "{} carries no schema version; it is not a design database. \
                 Build one with rtl-designdb.",
                path.display()
            ),
            OpenError::VersionMismatch { path, found, tool } => format!(
                "{} is schema version {found}; rtlscanner reads version {SCHEMA_VERSION}{}. \
                 Re-export it with a matching rtl-designdb.",
                path.display(),
                tool.as_ref().map(|t| format!(" (written by {t})")).unwrap_or_default()
            ),
        };
        write!(f, "{}", err(msg))
    }
}

/// An open, read-only design database.
pub struct Db {
    conn: Connection,
    path: PathBuf,
}

impl Db {
    /// Open `path` where it lies, read-only, and check the schema version.
    pub fn open(path: &Path) -> Result<Db, OpenError> {
        // try_exists, not exists: a directory this process may not traverse is
        // a permission problem, and reporting it as a missing file sends the
        // caller to check a path that is right.
        match path.try_exists() {
            Ok(true) => {}
            Ok(false) => {
                return Err(OpenError::NotFound {
                    path: path.to_path_buf(),
                    reason: "no such file".into(),
                });
            }
            Err(e) => {
                return Err(OpenError::NotFound {
                    path: path.to_path_buf(),
                    reason: e.to_string(),
                });
            }
        }

        let conn = Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .and_then(|c| {
            // SQLite opens lazily; force page 1 so a file that is not a
            // database is diagnosed here, in these words, rather than by
            // whichever query happens to run first.
            c.query_row("PRAGMA schema_version", [], |r| r.get::<_, i64>(0)).map(|_| c)
        })
        .map_err(|e| OpenError::NotADatabase { path: path.to_path_buf(), reason: e.to_string() })?;

        let db = Db { conn, path: path.to_path_buf() };
        db.check_version()?;
        Ok(db)
    }

    fn check_version(&self) -> Result<(), OpenError> {
        let found: Option<i64> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'schema_version'", [], |r| {
                r.get::<_, String>(0)
            })
            .ok()
            .and_then(|v| v.parse().ok());
        match found {
            Some(v) if v == SCHEMA_VERSION => Ok(()),
            Some(found) => Err(OpenError::VersionMismatch {
                path: self.path.clone(),
                found,
                tool: self.meta("tool"),
            }),
            None => Err(OpenError::NoSchemaVersion { path: self.path.clone() }),
        }
    }

    /// A `meta` value, when present. `top` is only recorded when the export was
    /// given `--top`, so its absence is ordinary; every other key of the seal is
    /// required, and a missing one means the file is not what it claims.
    pub fn meta(&self, key: &str) -> Option<String> {
        self.conn.query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0)).ok()
    }

    /// Every source file the export read, with its SHA-256, in path order.
    pub fn source_files(&self) -> Result<Vec<(String, String)>, String> {
        let mut stmt = self
            .conn
            .prepare("SELECT path, digest FROM src_file ORDER BY path")
            .map_err(|e| err(format!("reading src_file: {e}")))?;
        let rows = stmt
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .map_err(|e| err(format!("reading src_file: {e}")))?;
        rows.collect::<Result<Vec<_>, _>>().map_err(|e| err(format!("reading src_file: {e}")))
    }

    pub fn conn(&self) -> &Connection {
        &self.conn
    }
}

/// Building a design database to test against.
///
/// Exposed beyond the crate because the commands' own tests need one: covering
/// what a command does with a hierarchy_only export, or with a source file that
/// has moved on, must not depend on having an exporter — and building slang is
/// not a precondition for running tests.
pub mod fixture {
    use super::*;

    /// The seal a compliant export always writes. Only `top` is optional, so a
    /// fixture that omitted the rest would be testing a file the kit never
    /// writes — the same reason the DDL keeps its CHECK clauses.
    const SEAL: [(&str, &str); 14] = [
        ("tool", "rtl-designdb"),
        ("tool_version", "0.1.0"),
        ("slang_version", "v11.0"),
        ("producer_revision", "0000000"),
        ("analysis_status", "complete"),
        ("error_count", "0"),
        ("unresolved_count", "0"),
        ("empty_procedure_count", "0"),
        ("duplicate_path_count", "0"),
        ("recursion_count", "0"),
        ("truncated_call_count", "0"),
        ("checker_inst_count", "0"),
        ("unanalysed_inst_count", "0"),
        ("config_digest", "0000000000000000000000000000000000000000000000000000000000000000"),
    ];

    /// A database with the kit's own DDL, a complete seal claiming `version`,
    /// and whatever `seed` adds.
    ///
    /// The DDL is the kit's, verbatim, so what a test observes of a view is the
    /// view's real behaviour rather than an imitation of it; seeds write base
    /// tables only. Generating the file also keeps a fixture from pinning one
    /// design's shapes, which a committed `.db` would. Seeds override the seal
    /// with `INSERT OR REPLACE`, `meta.key` being the primary key.
    pub fn write_db(path: &Path, version: i64, seed: &[&str]) {
        let c = Connection::open(path).unwrap();
        for ddl in [
            include_str!("ddl/schema.sql"),
            include_str!("ddl/indexes.sql"),
            include_str!("ddl/views.sql"),
        ] {
            c.execute_batch(ddl).unwrap();
        }
        c.execute("INSERT INTO meta VALUES ('schema_version', ?1)", [version.to_string()]).unwrap();
        for (key, value) in SEAL {
            c.execute("INSERT INTO meta VALUES (?1, ?2)", [key, value]).unwrap();
        }
        for sql in seed {
            c.execute_batch(sql).unwrap();
        }
    }

    /// A fresh directory for one test. The tag distinguishes concurrent tests
    /// of one process, so it must be unique among the tests that use it.
    pub fn tmp(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("rtlscanner-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }
}

#[cfg(test)]
mod tests {
    use super::fixture::{tmp, write_db};
    use super::*;

    #[test]
    fn opens_a_design_database_read_only() {
        let dir = tmp("open");
        let path = dir.join("design.db");
        write_db(&path, SCHEMA_VERSION, &["INSERT INTO meta VALUES ('top', 'dut')"]);
        let before = std::fs::read(&path).unwrap();

        let db = Db::open(&path).unwrap();
        assert_eq!(db.meta("top").as_deref(), Some("dut"));
        assert_eq!(db.meta("tool").as_deref(), Some("rtl-designdb"));
        assert_eq!(db.meta("absent"), None);

        assert_eq!(std::fs::read(&path).unwrap(), before, "opening modified the user's file");
    }

    #[test]
    fn an_unknown_schema_version_is_refused_not_guessed() {
        let dir = tmp("version");
        // Below and above: a newer file is refused too, since a version this
        // reader does not know is one whose rows may claim something else.
        for bad in [1, 13, 17, 18, 20] {
            let path = dir.join(format!("v{bad}.db"));
            write_db(&path, bad, &[]);
            let Err(e) = Db::open(&path) else { panic!("v{bad} was opened, not refused") };
            assert!(matches!(e, OpenError::VersionMismatch { found, .. } if found == bad));

            let said = e.to_string();
            assert!(said.contains(&format!("schema version {bad}")), "{said}");
            assert!(said.contains(&format!("version {SCHEMA_VERSION}")), "{said}");
            assert!(said.contains("rtl-designdb"), "{said}");
        }
    }

    #[test]
    fn a_file_that_is_not_a_design_database_says_which_way_it_is_not() {
        let dir = tmp("notdb");

        let empty = dir.join("empty.db");
        Connection::open(&empty).unwrap().execute_batch("CREATE TABLE t(x)").unwrap();
        assert!(matches!(Db::open(&empty), Err(OpenError::NoSchemaVersion { .. })));

        let junk = dir.join("junk.db");
        std::fs::write(&junk, b"not a database at all").unwrap();
        assert!(matches!(Db::open(&junk), Err(OpenError::NotADatabase { .. })));

        let Err(e) = Db::open(&dir.join("nope.db")) else { panic!("a missing file was opened") };
        assert!(matches!(e, OpenError::NotFound { .. }));
        assert!(e.to_string().contains("no such file"));
    }

    #[test]
    fn source_files_come_back_with_digests() {
        let dir = tmp("srcfile");
        let path = dir.join("design.db");
        write_db(
            &path,
            SCHEMA_VERSION,
            &["INSERT INTO src_file(id, path, digest) VALUES (1, '/p/dut.sv', 'abc123')"],
        );
        let db = Db::open(&path).unwrap();
        assert_eq!(db.source_files().unwrap(), vec![("/p/dut.sv".into(), "abc123".into())]);
    }
}
