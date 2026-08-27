//! `tree` and `find` — how a caller who does not yet know a name gets one.
//!
//! Every other command takes a hierarchical path, which assumes the caller
//! already has one. These two are where that path comes from: `tree` says what
//! the design is made of, `find` says where a name lives. Neither analyses
//! anything; they are the design's own table of contents.

use designdb::{Connection, Db, schema};
use serde_json::{Value, json};

use crate::cone_result::resolve_limit;
use crate::envelope::{CommandResult, Diagnostic};

/// One level of the elaborated tree, as a caller sees it.
pub struct Level {
    path: String,
    /// The segment's own name. Kept rather than cut off the path: an escaped
    /// identifier may contain the separator, so a path does not split back.
    name: String,
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
    /// Whether the depth bound cut the walk short — there is more below what
    /// is shown, and `--depth 0` reaches it.
    deeper: bool,
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
            // A separate fact from the output being clipped: what is below the
            // depth bound was never walked, so no limit reaches it.
            "depth_truncated": self.deeper,
            "limit": if self.limit == usize::MAX { 0 } else { self.limit },
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let mut out = String::new();
        for level in self.shown() {
            let name = &level.name;
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
        if self.deeper {
            out.push_str(&format!(
                "\nstopped at depth {}; --depth 0 for all\n",
                self.max_depth.unwrap_or(0)
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
    let Some(top) = schema::node(c, root)? else {
        return Err(format!("no tree level with id {root}"));
    };
    let mut levels = Vec::new();
    let mut deeper = false;
    // Reversed on push so siblings come off in name order.
    let mut stack = vec![(top, root_path.to_string(), 0u32)];

    while let Some((row, path, depth)) = stack.pop() {
        let children = schema::children_of(c, row.node_id)?;
        levels.push(Level {
            path: path.clone(),
            name: row.node_name.clone(),
            kind: row.node_kind.clone(),
            module: row.module_name.clone().or(row.def_name.clone()),
            depth,
            nets: schema::net_count(c, row.node_id)?,
            children: children.len(),
        });
        if max_depth.is_some_and(|max| depth >= max) {
            // Stopping short is part of the answer: an answer that looked
            // whole and was not is the one a caller cannot check.
            deeper |= !children.is_empty();
            continue;
        }
        for child in children.into_iter().rev() {
            let below = format!("{path}.{}", child.node_name);
            stack.push((child, below, depth + 1));
        }
    }
    Ok(Tree { root: root_path.to_string(), max_depth, levels, deeper, limit: resolve_limit(limit) })
}

/// What a name search turned up.
pub struct Found {
    pattern: String,
    kind: Kind,
    hits: Vec<Hit>,
    /// Whether the query itself stopped early, which is a different fact from
    /// the answer being clipped: what is beyond was never looked at, so no
    /// limit reaches it.
    capped: bool,
    /// Matches that lie under another top than the one chosen. They are real
    /// and they are not addressable from here, so they are set aside and
    /// counted rather than silently dropped.
    outside_root: usize,
    limit: usize,
}

impl Found {
    /// What a caller should know about a pattern that found nothing.
    ///
    /// A glob is matched against the name a net has inside its instance, not
    /// against its path, so a pattern written as a path matches nothing and
    /// says so with the same empty answer as a name that is simply not there.
    /// The two need telling apart: one is a fact about the design, the other
    /// is a question this command does not take.
    pub fn notes(&self) -> Vec<Diagnostic> {
        if !self.hits.is_empty() || !self.pattern.contains(['.', '/']) {
            return Vec::new();
        }
        vec![Diagnostic::warning(format!(
            "'{}' is matched against a name, not a path — a hierarchical pattern finds \
             nothing here whatever the design holds; drop the scopes and glob the name \
             ('*{}')",
            self.pattern,
            self.pattern.rsplit(['.', '/']).next().unwrap_or(&self.pattern),
        ))]
    }
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
            "outside_root": self.outside_root,
            "limit": if self.limit == usize::MAX { 0 } else { self.limit },
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let shown = &self.hits[..self.hits.len().min(self.limit)];
        let mut out = String::new();
        if shown.is_empty() {
            out.push_str(&format!("no {} matches '{}'\n", self.kind.tag(), self.pattern));
        }
        for hit in shown {
            // What a hit is, where that is not the kind asked for: a black box
            // and a module read alike otherwise, and a trace ending in one is
            // a different fact from ending in the other.
            let what = match self.kind {
                Kind::Net => String::new(),
                _ => format!("  ({})", hit.what),
            };
            out.push_str(&format!(
                "  {}{what}{}\n",
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
            out.push_str(&format!(
                "note: the search stopped after {SEARCH_CAP} matches; narrow the pattern \
                 to see the rest\n"
            ));
        }
        if self.outside_root > 0 {
            out.push_str(&format!(
                "note: {} match(es) lie under another top; --top names it\n",
                self.outside_root
            ));
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
    let (hits, outside_root, capped) = match kind {
        Kind::Net => nets(c, anchor, pattern)?,
        Kind::Instance => instances(c, anchor, pattern)?,
        Kind::Module => {
            // A definition belongs to no top: it is the thing occurrences are
            // made from, so none of them is out of scope.
            let (rows, capped) = schema::modules_matching(c, pattern, SEARCH_CAP)?;
            let hits = rows
                .into_iter()
                .map(|(name, def_kind, count)| Hit {
                    path: name,
                    what: def_kind,
                    detail: Some(format!("{count} occurrence(s)")),
                })
                .collect();
            (hits, 0, capped)
        }
    };
    Ok(Found {
        pattern: pattern.to_string(),
        kind,
        capped,
        outside_root,
        hits,
        limit: resolve_limit(limit),
    })
}

fn nets(
    c: &Connection,
    anchor: &designdb::resolve::Anchor,
    pattern: &str,
) -> Result<(Vec<Hit>, usize, bool), String> {
    let (rows, capped) = schema::nets_matching(c, pattern, SEARCH_CAP)?;
    let mut hits = Vec::new();
    let mut outside = 0;
    for row in rows {
        // A net under another top has a path, and not one that leads there
        // from here. Offering it would break the property these two commands
        // exist for: what `find` returns, the others accept.
        let Some(scope) = scope_under(c, anchor, row.inst_id)? else {
            outside += 1;
            continue;
        };
        hits.push(Hit {
            path: format!("{scope}.{}", row.net_name),
            what: "net".into(),
            detail: row.width.map(|w| format!("{} [{w} bit(s)]", row.decl_kind)),
        });
    }
    Ok((hits, outside, capped))
}

/// The path of one instance under the chosen root, or `None` if it is not.
fn scope_under(
    c: &Connection,
    anchor: &designdb::resolve::Anchor,
    inst: i64,
) -> Result<Option<String>, String> {
    let Some(spine) = designdb::resolve::path_below_root(c, anchor.root, inst)? else {
        return Ok(None);
    };
    let mut parts = vec![anchor.root_name.clone()];
    parts.extend(spine);
    Ok(Some(parts.join(".")))
}

fn instances(
    c: &Connection,
    anchor: &designdb::resolve::Anchor,
    pattern: &str,
) -> Result<(Vec<Hit>, usize, bool), String> {
    let (rows, capped) = schema::nodes_matching(c, pattern, SEARCH_CAP)?;
    let mut hits = Vec::new();
    let mut outside = 0;
    for row in rows {
        // A level under another top is real and is not addressable from here.
        let Some(path) = scope_under(c, anchor, row.node_id)? else {
            outside += 1;
            continue;
        };
        hits.push(Hit {
            path,
            what: row.node_kind.clone(),
            detail: row.module_name.clone().or(row.def_name.clone()),
        });
    }
    Ok((hits, outside, capped))
}
