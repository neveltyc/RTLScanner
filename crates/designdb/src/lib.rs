//! Reading an rtl-designdb `design.db`.
//!
//! The database is read through its consumption contract: schema v19's `v_*`
//! views, instance-level — every net, statement and dependency row hangs off
//! the elaborated occurrence, and `v_driver`/`v_load` carry boundary crossings
//! as rows of their own. Names in a row are relative to the row's instance, so
//! a full path is the instance's tree path plus the row's name; that is scope,
//! not hierarchy, and the two are assembled rather than split apart.
//!
//! Two rules the kit states and this reader keeps. Point queries seek: the
//! closure is the consumer's, one query per hop, so a scan per hop would be a
//! scan per net in a cone. And no transitive closure lives here — a fan-in cone
//! is the engine's walk over these rows, not a recursive query inside them.
//!
//! `v_conn_arc` is scaffolding the kit may change without a version bump, and
//! is deliberately never queried.

pub mod digest;
pub mod open;
pub mod schema;

pub use open::{Db, OpenError, SCHEMA_VERSION};
pub use schema::DbInfo;

/// Prefix on every message from this reader, so a failure names its layer.
const ERR_PREFIX: &str = "designdb";

/// Prefix a message for the user.
pub(crate) fn err(msg: impl AsRef<str>) -> String {
    format!("{ERR_PREFIX}: {}", msg.as_ref())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn err_carries_the_reader_prefix() {
        assert_eq!(err("boom"), "designdb: boom");
    }
}
