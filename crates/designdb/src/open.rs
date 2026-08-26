//! Opening a `design.db`.
//!
//! The file is stock SQLite — rtl-designdb bundles its own copy of the library
//! precisely so anything speaking SQL can read the result. What has to be
//! checked is the schema version: this reader was written against exactly one,
//! and an unrecognised version is refused rather than parsed on the assumption
//! the layout held. Reading a moved column would produce a confident wrong
//! answer, which is worse than no answer.

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

/// An open, read-only design database.
pub struct Db {
    conn: Connection,
    path: PathBuf,
}

impl std::fmt::Debug for Db {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // `Connection` is not Debug, and the path is the only identifying part.
        write!(f, "Db({})", self.path.display())
    }
}

impl Db {
    /// Open `path` where it lies, read-only, and check the schema version.
    pub fn open(path: &Path) -> Result<Db, String> {
        if !path.exists() {
            return Err(err(format!("{}: no such file.", path.display())));
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
        .map_err(|e| err(format!("{} did not open as a database: {e}", path.display())))?;

        let db = Db { conn, path: path.to_path_buf() };
        db.check_version()?;
        Ok(db)
    }

    fn check_version(&self) -> Result<(), String> {
        let got: Option<i64> = self
            .conn
            .query_row("SELECT value FROM meta WHERE key = 'schema_version'", [], |r| {
                r.get::<_, String>(0)
            })
            .ok()
            .and_then(|v| v.parse().ok());
        match got {
            Some(v) if v == SCHEMA_VERSION => Ok(()),
            Some(v) => Err(err(format!(
                "{} is schema version {v}; rtlscanner reads version {SCHEMA_VERSION}{}. \
                 Re-export it with a matching rtl-designdb.",
                self.path.display(),
                self.meta("tool").map(|t| format!(" (written by {t})")).unwrap_or_default()
            ))),
            None => Err(err(format!(
                "{} carries no schema version; it is not a design database. \
                 Build one with rtl-designdb.",
                self.path.display()
            ))),
        }
    }

    /// A `meta` value, when present. `top` is only recorded when the export was
    /// given `--top`, so its absence is ordinary.
    pub fn meta(&self, key: &str) -> Option<String> {
        self.conn
            .query_row("SELECT value FROM meta WHERE key = ?1", [key], |r| r.get(0))
            .ok()
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

    pub fn path(&self) -> &Path {
        &self.path
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A database with the kit's own DDL and a `meta` claiming `version`.
    ///
    /// The DDL is the kit's, verbatim, so what a test observes of a view is the
    /// view's real behaviour rather than an imitation of it; seeds write base
    /// tables only. Generating the file also keeps a fixture from pinning one
    /// design's shapes, which a committed `.db` would.
    pub fn write_db(path: &Path, version: i64, seed: &[&str]) {
        let c = Connection::open(path).unwrap();
        for ddl in [
            include_str!("ddl/schema.sql"),
            include_str!("ddl/indexes.sql"),
            include_str!("ddl/views.sql"),
        ] {
            c.execute_batch(ddl).unwrap();
        }
        c.execute(
            "INSERT INTO meta VALUES ('schema_version', ?1), ('tool', 'rtl-designdb')",
            [version.to_string()],
        )
        .unwrap();
        for sql in seed {
            c.execute_batch(sql).unwrap();
        }
    }

    /// A fresh directory for one test. The tag distinguishes concurrent tests
    /// of one process, so it must be unique within the crate.
    pub fn tmp(tag: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("rtlscanner-designdb-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        p
    }

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
            let e = Db::open(&path).unwrap_err();
            assert!(e.contains(&format!("schema version {bad}")), "{e}");
            assert!(e.contains(&format!("version {SCHEMA_VERSION}")), "{e}");
            assert!(e.contains("rtl-designdb"), "{e}");
        }
    }

    #[test]
    fn a_file_that_is_not_a_design_database_says_so() {
        let dir = tmp("notdb");

        let empty = dir.join("empty.db");
        Connection::open(&empty).unwrap().execute_batch("CREATE TABLE t(x)").unwrap();
        assert!(Db::open(&empty).unwrap_err().contains("carries no schema version"));

        let junk = dir.join("junk.db");
        std::fs::write(&junk, b"not a database at all").unwrap();
        assert!(Db::open(&junk).unwrap_err().contains("did not open as a database"));

        assert!(Db::open(&dir.join("nope.db")).unwrap_err().contains("no such file"));
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
