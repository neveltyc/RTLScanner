//! `tree` and `find` — how a caller who does not yet know a name gets one.
//!
//! Every other command takes a hierarchical path, which assumes the caller
//! already has one. These two are where that path comes from: `tree` says what
//! the design is made of, `find` says where a name lives. Neither analyses
//! anything; they are the design's own table of contents.

use designdb::{Connection, Db, schema};
use serde_json::{Value, json};

use crate::cone_result::resolve_limit;
use crate::envelope::CommandResult;

/// One level of the elaborated tree, as a caller sees it.
pub struct Level {
    path: String,
    /// `root | instance | generate | primitive | unresolved | package`. A trace
    /// that ends at an `unresolved` level ended at a black box, which is a
    /// different fact from ending at a signal nothing drives.
    kind: String,
    /// The definition this level came from, where it has one. A generate block
    /// is a naming level and has none.
    module: Option<String>,
    depth: u32,
    /// How many nets this occurrence declares, so a caller can tell a level
    /// worth descending into from a level of pure structure.
    nets: i64,
    children: usize,
}

pub struct Tree {
    root: String,
    max_depth: Option<u32>,
    levels: Vec<Level>,
    limit: usize,
}

impl Tree {
    fn shown(&self) -> &[Level] {
        &self.levels[..self.levels.len().min(self.limit)]
    }
}

impl CommandResult for Tree {
    fn to_json(&self) -> (Value, Value) {
        let data = json!({
            "root": self.root,
            "max_depth": self.max_depth,
            "levels": self.shown().iter().map(|l| json!({
                "path": l.path,
                "kind": l.kind,
                "module": l.module,
                "depth": l.depth,
                "nets": l.nets,
                "children": l.children,
            })).collect::<Vec<_>>(),
        });
        let summary = json!({
            "levels": self.levels.len(),
            "shown": self.shown().len(),
            "truncated": self.levels.len() > self.limit,
            "limit": if self.limit == usize::MAX { 0 } else { self.limit },
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let mut out = String::new();
        for level in self.shown() {
            let name = level.path.rsplit('.').next().unwrap_or(&level.path);
            let what = match (&level.module, level.kind.as_str()) {
                (Some(m), _) => m.clone(),
                (None, kind) => format!("({kind})"),
            };
            out.push_str(&format!(
                "{:indent$}{name}  {what}{}\n",
                "",
                match level.nets {
                    0 => String::new(),
                    n => format!("  [{n} net(s)]"),
                },
                indent = level.depth as usize * 2,
            ));
        }
        if self.levels.len() > self.limit {
            out.push_str(&format!(
                "\ntruncated: {}/{} levels; --limit 0 for all\n",
                self.shown().len(),
                self.levels.len()
            ));
        }
        out
    }
}

/// Walk the tree under one node, depth first.
///
/// Depth first because a tree is read as a tree: a level's children belong
/// under it, and breadth-first order would print every cousin between a parent
/// and its child.
pub fn tree(
    db: &Db,
    root: i64,
    root_path: &str,
    max_depth: Option<u32>,
    limit: Option<i64>,
) -> Result<Tree, String> {
    let c = db.conn();
    let mut levels = Vec::new();
    // Reversed on push so siblings come off in name order.
    let mut stack = vec![(root, root_path.to_string(), 0u32)];

    while let Some((node, path, depth)) = stack.pop() {
        let Some(row) = schema::node(c, node)? else { continue };
        let children = schema::children_of(c, node)?;
        levels.push(Level {
            path: path.clone(),
            kind: row.node_kind.clone(),
            module: row.module_name.clone().or(row.def_name.clone()),
            depth,
            nets: match row.inst_id {
                Some(inst) => schema::net_count(c, inst)?,
                None => 0,
            },
            children: children.len(),
        });
        if max_depth.is_some_and(|max| depth >= max) {
            continue;
        }
        for child in children.into_iter().rev() {
            let below = format!("{path}.{}", child.node_name);
            stack.push((child.node_id, below, depth + 1));
        }
    }
    Ok(Tree { root: root_path.to_string(), max_depth, levels, limit: resolve_limit(limit) })
}

/// What a name search turned up.
pub struct Found {
    pattern: String,
    kind: Kind,
    hits: Vec<Hit>,
    /// Whether the query itself stopped early, which is a different fact from
    /// the answer being clipped: what is beyond was never looked at.
    capped: bool,
    limit: usize,
}

struct Hit {
    path: String,
    what: String,
    detail: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Net,
    Instance,
    Module,
}

impl Kind {
    fn tag(self) -> &'static str {
        match self {
            Kind::Net => "net",
            Kind::Instance => "instance",
            Kind::Module => "module",
        }
    }
}

impl CommandResult for Found {
    fn to_json(&self) -> (Value, Value) {
        let shown = &self.hits[..self.hits.len().min(self.limit)];
        let data = json!({
            "pattern": self.pattern,
            "kind": self.kind.tag(),
            "hits": shown.iter().map(|h| json!({
                "path": h.path,
                "what": h.what,
                "detail": h.detail,
            })).collect::<Vec<_>>(),
        });
        let summary = json!({
            "hits": self.hits.len(),
            "shown": shown.len(),
            "truncated": self.hits.len() > self.limit,
            // The search stopped before the design ran out, so there may be
            // matches nobody has looked for yet.
            "capped": self.capped,
            "limit": if self.limit == usize::MAX { 0 } else { self.limit },
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let shown = &self.hits[..self.hits.len().min(self.limit)];
        let mut out = String::new();
        for hit in shown {
            out.push_str(&format!(
                "  {}{}\n",
                hit.path,
                hit.detail.as_deref().map(|d| format!("  {d}")).unwrap_or_default()
            ));
        }
        if self.hits.len() > self.limit {
            out.push_str(&format!(
                "\ntruncated: {}/{} hits; --limit 0 for all\n",
                shown.len(),
                self.hits.len()
            ));
        }
        if self.capped {
            out.push_str("note: the search stopped early; narrow the pattern to see the rest\n");
        }
        out
    }
}

/// How many matches a search reads before giving up on the rest.
///
/// A pattern of `*` on a large design matches every net; reading them all to
/// show twenty would spend the design's whole size on a typo.
const SEARCH_CAP: usize = 5000;

/// Find nets, instances or definitions whose name matches a glob.
pub fn find(
    db: &Db,
    anchor: &designdb::resolve::Anchor,
    pattern: &str,
    kind: Kind,
    limit: Option<i64>,
) -> Result<Found, String> {
    let c = db.conn();
    let hits = match kind {
        Kind::Net => nets(c, anchor, pattern)?,
        Kind::Instance => instances(c, anchor, pattern)?,
        Kind::Module => schema::modules_matching(c, pattern, SEARCH_CAP)?
            .into_iter()
            .map(|(name, def_kind, count)| Hit {
                path: name,
                what: def_kind,
                detail: Some(format!("{count} occurrence(s)")),
            })
            .collect(),
    };
    Ok(Found {
        pattern: pattern.to_string(),
        kind,
        capped: hits.len() >= SEARCH_CAP,
        hits,
        limit: resolve_limit(limit),
    })
}

fn nets(
    c: &Connection,
    anchor: &designdb::resolve::Anchor,
    pattern: &str,
) -> Result<Vec<Hit>, String> {
    let mut hits = Vec::new();
    for row in schema::nets_matching(c, pattern, SEARCH_CAP)? {
        let scope = crate::trace::instance_path(c, anchor, row.inst_id, '.')?;
        hits.push(Hit {
            path: format!("{scope}.{}", row.net_name),
            what: "net".into(),
            detail: row.width.map(|w| format!("{} [{w} bit(s)]", row.decl_kind)),
        });
    }
    Ok(hits)
}

fn instances(
    c: &Connection,
    anchor: &designdb::resolve::Anchor,
    pattern: &str,
) -> Result<Vec<Hit>, String> {
    let mut hits = Vec::new();
    for row in schema::nodes_matching(c, pattern, SEARCH_CAP)? {
        // A level below the chosen root belongs to another top, and naming it
        // by a path that does not lead there would be worse than omitting it.
        let Some(spine) = designdb::resolve::path_below_root(c, anchor.root, row.node_id)? else {
            continue;
        };
        let mut parts = vec![anchor.root_name.clone()];
        parts.extend(spine);
        hits.push(Hit {
            path: parts.join("."),
            what: row.node_kind.clone(),
            detail: row.module_name.clone().or(row.def_name.clone()),
        });
    }
    Ok(hits)
}
