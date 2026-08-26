//! The contract surface: rows of the `v_*` views, mapped by column name.
//!
//! Columns are listed explicitly and read by index into that list, never by
//! position in the view — a schema change then fails to prepare rather than
//! transposing two same-typed neighbours silently.

use rusqlite::Connection;

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
    .map_err(|e| err(format!("reading v_db_info: {e}")))
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
            &["INSERT OR REPLACE INTO meta VALUES ('top', 'dut'), \
               ('analysis_status', 'partial'), ('error_count', '4'), \
               ('empty_procedure_count', '2')"],
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
    fn a_seal_missing_a_required_key_is_a_failure_not_a_zero() {
        let dir = tmp("dbinfo-hollow");
        let path = dir.join("design.db");
        // A file that opens and passes the version gate, with no seal behind
        // it. Reported as zero counts it would read as an export with nothing
        // wrong in it — the opposite of what it is.
        write_db(&path, crate::SCHEMA_VERSION, &["DELETE FROM meta WHERE key = 'error_count'"]);
        let db = Db::open(&path).unwrap();

        let e = db_info(db.conn()).unwrap_err();
        assert!(e.contains("v_db_info"), "{e}");
    }
}
