//! Turning a hierarchical path into the net it names.
//!
//! Two things a path is not. It is not a hierarchy: a generate block is a level
//! of the path but not a level of the instance tree, and a net declared inside
//! one belongs to the enclosing instance under a name that carries the generate
//! segments. And it does not round-trip: an escaped identifier may hold a `.`,
//! so a path is assembled segment by segment and matched whole, never split
//! back apart on a separator and trusted.

use crate::err;
use crate::schema::{self, NetRow};

/// Separators a path may be written with. `/` is what some waveform tools
/// spell a hierarchy with, and a caller pasting one in should not have to
/// translate it first.
const SEPARATORS: [char; 2] = ['.', '/'];

/// Split a path into segments, skipping a leading separator.
pub fn segments(path: &str) -> Vec<&str> {
    path.split(SEPARATORS).filter(|s| !s.is_empty()).collect()
}

/// Where the design sits, and what to strip off a path before looking in it.
///
/// A path from a waveform tool is anchored at a testbench the design has never
/// heard of. Stripping that prefix is the caller's to state — the design has no
/// way to recognise a scope that is not in it — so the anchor carries it.
#[derive(Debug, Clone)]
pub struct Anchor {
    /// The root instance's node id.
    pub root: i64,
    /// The root's own name, as the design spells it.
    pub root_name: String,
    /// A prefix to remove from an incoming path, without its trailing
    /// separator. Empty means paths are already design-relative.
    pub strip_prefix: String,
}

/// A net, with where it sits.
#[derive(Debug, Clone)]
pub struct ResolvedSignal {
    /// The instance whose body declares it.
    pub inst: i64,
    /// The path from the root down to that instance, generate levels included.
    pub below_root: Vec<String>,
    /// The net's name relative to its instance.
    pub local: String,
    pub net: NetRow,
}

impl ResolvedSignal {
    /// The design-relative path of this net.
    pub fn path(&self, root_name: &str, sep: char) -> String {
        let mut parts = vec![root_name.to_string()];
        parts.extend(self.below_root.iter().cloned());
        parts.push(self.local.replace('.', &sep.to_string()));
        parts.join(&sep.to_string())
    }
}

/// Choose the root to resolve against.
///
/// A design with one top needs no argument. With several, the caller names one:
/// picking for them would answer about a different design than the one they
/// meant, and there is nothing in the file that says which.
pub fn anchor(
    c: &rusqlite::Connection,
    top: Option<&str>,
    strip_prefix: Option<&str>,
) -> Result<Anchor, String> {
    let roots = schema::roots(c)?;
    let chosen = match (top, roots.len()) {
        (Some(name), _) => roots.iter().find(|r| r.node_name == name).ok_or_else(|| {
            let have: Vec<&str> = roots.iter().map(|r| r.node_name.as_str()).collect();
            err(format!("no top named '{name}'; this database has: {}", have.join(", ")))
        })?,
        (None, 1) => &roots[0],
        (None, 0) => return Err(err("this database elaborated no top")),
        (None, _) => {
            let have: Vec<&str> = roots.iter().map(|r| r.node_name.as_str()).collect();
            return Err(err(format!(
                "this database has {} tops ({}); name one with --top",
                roots.len(),
                have.join(", ")
            )));
        }
    };
    Ok(Anchor {
        root: chosen.node_id,
        root_name: chosen.node_name.clone(),
        strip_prefix: strip_prefix.unwrap_or("").trim_end_matches(SEPARATORS).to_string(),
    })
}

/// Remove the anchor's prefix from a path.
///
/// The remainder must start at a segment boundary: `tb.u_dut` must not match
/// the front of `tb.u_dutch`, which is a different scope whose name happens to
/// begin the same way.
pub fn strip_prefix<'a>(anchor: &Anchor, path: &'a str) -> Result<&'a str, String> {
    if anchor.strip_prefix.is_empty() {
        return Ok(path);
    }
    let rest = path
        .strip_prefix(anchor.strip_prefix.as_str())
        .ok_or_else(|| err(format!("'{path}' is not under '{}'", anchor.strip_prefix)))?;
    match rest.chars().next() {
        None => Ok(""),
        Some(c) if SEPARATORS.contains(&c) => Ok(&rest[c.len_utf8()..]),
        Some(_) => Err(err(format!("'{path}' is not under '{}'", anchor.strip_prefix))),
    }
}

/// The path from `root` down to `inst`, or `None` if it is not below it.
pub fn path_below_root(
    c: &rusqlite::Connection,
    root: i64,
    inst: i64,
) -> Result<Option<Vec<String>>, String> {
    let mut parts = Vec::new();
    let mut at = inst;
    loop {
        if at == root {
            parts.reverse();
            return Ok(Some(parts));
        }
        let Some(node) = schema::node(c, at)? else { return Ok(None) };
        let Some(parent) = node.parent_node_id else { return Ok(None) };
        parts.push(node.node_name);
        at = parent;
    }
}

/// Walk `segments` down from `root` and resolve `leaf` as a net there.
///
/// The two cursors are the point: `node` descends every level of the path,
/// while `inst` only advances at an instance. Generate levels fold into the
/// name instead, which is where a net declared in one actually lives.
fn walk(
    c: &rusqlite::Connection,
    root: i64,
    segments: &[&str],
    leaf: &str,
) -> Result<Option<ResolvedSignal>, String> {
    let Some(root_node) = schema::node(c, root)? else { return Ok(None) };
    let mut node = root_node.node_id;
    let mut inst = match root_node.inst_id {
        Some(i) => i,
        None => return Ok(None),
    };
    let mut below: Vec<String> = Vec::new();
    let mut folded: Vec<String> = Vec::new();

    let mut rest = segments;
    while let Some((first, tail)) = rest.split_first() {
        match schema::child_node(c, node, first)? {
            Some(child) => match child.node_kind.as_str() {
                "instance" | "root" => {
                    let Some(child_inst) = child.inst_id else { return Ok(None) };
                    below.append(&mut folded);
                    below.push(child.node_name.clone());
                    node = child.node_id;
                    inst = child_inst;
                    rest = tail;
                }
                // A generate level names a scope, not an instance: its nets
                // belong to the instance around it, under a name that carries
                // this segment.
                "generate" => {
                    folded.push(child.node_name.clone());
                    node = child.node_id;
                    rest = tail;
                }
                // A black box or a gate. The export has none of its nets, so
                // nothing below it resolves — reported like any name that is
                // not there, since the caller's next move is the same.
                _ => return Ok(None),
            },
            None => {
                if !folded.is_empty() {
                    return Ok(None);
                }
                match detour(c, node, inst, first)? {
                    None => break,
                    Some(None) => return Ok(None),
                    Some(Some(next)) => {
                        if next != node {
                            let Some(spine) = path_below_root(c, root, next)? else {
                                return Ok(None);
                            };
                            below = spine;
                            node = next;
                            inst = next;
                        }
                        rest = tail;
                    }
                }
            }
        }
    }

    // Whatever is left is not a level of the tree: subroutine scopes have no
    // node, and their nets are named through them.
    let mut local = folded;
    local.extend(rest.iter().map(|s| s.to_string()));
    local.push(leaf.to_string());
    let local = local.join(".");

    if let Some(net) = schema::net_by_name(c, inst, &local)? {
        return Ok(Some(ResolvedSignal { inst, below_root: below, local, net }));
    }
    // An escaped identifier is stored as SystemVerilog spells it, and a caller
    // may have written it either way.
    if let Some(bare) = local.strip_prefix('\\') {
        let bare = bare.trim_end();
        if let Some(net) = schema::net_by_name(c, inst, bare)? {
            return Ok(Some(ResolvedSignal {
                inst,
                below_root: below,
                local: bare.to_string(),
                net,
            }));
        }
    }
    Ok(None)
}

/// Where a segment that is not a level of the tree carries on from.
///
/// Two detours, and they belong here rather than in `walk` because `diagnose`
/// must take the same ones: a segment the walk crossed is not the segment that
/// failed, and an error naming it sends the caller to correct the wrong word.
///
/// `None` is no detour. `Some(None)` is one with nothing behind it — an
/// interface array segment the export does not resolve per occurrence.
/// `Some(Some(node))` is where to carry on, which for a modport is the node
/// already there: a modport views an interface's nets without holding any, so
/// `b.mst.vld` and `b.vld` name one net and only the second is its name.
fn detour(
    c: &rusqlite::Connection,
    node: i64,
    inst: i64,
    segment: &str,
) -> Result<Option<Option<i64>>, String> {
    // An interface port is a scope in a path but a terminal in the database,
    // so a member reference through one goes to the interface it is bound to.
    if let Some(term) = schema::interface_terminal(c, inst, segment)? {
        return Ok(Some(schema::iface_target(c, term.term_id)?.flatten()));
    }
    Ok(schema::names_modport(c, inst, segment)?.then_some(Some(node)))
}

/// What a path did not resolve to, with enough of the design to correct it.
#[derive(Debug, Clone)]
pub struct Unresolved {
    /// The longest prefix of the path that named something.
    pub valid_prefix: Vec<String>,
    /// The segment that did not.
    pub failing_segment: String,
    /// What that level does contain.
    pub candidates: Vec<String>,
}

/// Resolve a design-relative path to the net it names.
///
/// The leaf is found by trying each split from the right: an escaped identifier
/// may contain a separator, so which part of the path is a scope and which is a
/// name cannot be decided by looking at the text.
pub fn resolve(
    c: &rusqlite::Connection,
    anchor: &Anchor,
    path: &str,
) -> Result<Result<ResolvedSignal, Unresolved>, String> {
    let path = strip_prefix(anchor, path)?;
    let mut segments = segments(path);

    // A path may or may not repeat the root's own name; both spell the same
    // net, and the design is what says which one this is.
    if segments.first() == Some(&anchor.root_name.as_str()) {
        segments.remove(0);
    }
    if segments.is_empty() {
        return Ok(Err(Unresolved {
            valid_prefix: Vec::new(),
            failing_segment: path.to_string(),
            candidates: names_under(c, anchor.root)?,
        }));
    }

    for split in (0..segments.len()).rev() {
        let leaf = segments[split..].join(".");
        if let Some(found) = walk(c, anchor.root, &segments[..split], &leaf)? {
            return Ok(Ok(found));
        }
    }
    Ok(Err(diagnose(c, anchor, &segments)?))
}

/// Walk as far as the path does resolve, and report what is at the level it
/// stopped in. A caller correcting a name needs the level, not the failure.
fn diagnose(
    c: &rusqlite::Connection,
    anchor: &Anchor,
    segments: &[&str],
) -> Result<Unresolved, String> {
    let mut node = anchor.root;
    let mut inst = schema::node(c, anchor.root)?.and_then(|n| n.inst_id);
    let mut valid = Vec::new();

    for (i, segment) in segments.iter().enumerate() {
        // The last segment is the name, not a scope: a path failing there
        // failed to name a net, and the level above it is what to list.
        if i + 1 == segments.len() {
            break;
        }
        let crossed = match schema::child_node(c, node, segment)? {
            Some(child) => Some((child.node_id, child.inst_id)),
            // A generate level has no instance, and `walk` takes no detour
            // from one either.
            None => match inst {
                None => None,
                Some(at) => match detour(c, node, at, segment)? {
                    Some(Some(next)) => Some((next, Some(next))),
                    _ => None,
                },
            },
        };
        match crossed {
            Some((next_node, next_inst)) => {
                node = next_node;
                inst = next_inst;
                valid.push((*segment).to_string());
            }
            None => {
                return Ok(Unresolved {
                    valid_prefix: valid,
                    failing_segment: (*segment).to_string(),
                    candidates: names_under(c, node)?,
                });
            }
        }
    }
    Ok(Unresolved {
        valid_prefix: valid,
        failing_segment: segments.last().map(|s| s.to_string()).unwrap_or_default(),
        candidates: names_under(c, node)?,
    })
}

/// The scopes and nets a level holds, for a caller correcting a name.
///
/// A generate level declares no instance, so its nets belong to the instance
/// around it under names carrying the generate segments. Walking up to that
/// instance and stripping the prefix is what makes a name inside a generate
/// block correctable at all.
fn names_under(c: &rusqlite::Connection, node: i64) -> Result<Vec<String>, String> {
    let mut names: Vec<String> =
        schema::children_of(c, node)?.into_iter().map(|n| n.node_name).collect();

    let mut at = Some(node);
    let mut prefix: Vec<String> = Vec::new();
    while let Some(id) = at {
        let Some(row) = schema::node(c, id)? else { break };
        if let Some(inst) = row.inst_id {
            let scope = prefix.join(".");
            names.extend(schema::nets_of_instance(c, inst)?.into_iter().filter_map(|n| {
                match scope.is_empty() {
                    true => Some(n.net_name),
                    false => n.net_name.strip_prefix(&format!("{scope}.")).map(str::to_string),
                }
            }));
            break;
        }
        prefix.insert(0, row.node_name);
        at = row.parent_node_id;
    }
    names.sort();
    names.dedup();
    Ok(names)
}

/// The names closest to `wanted`, for "did you mean".
///
/// Edit distance bounded by a third of the name's length: a suggestion further
/// off than that is noise, and offering everything is the same as offering
/// nothing.
pub fn close_matches(wanted: &str, candidates: &[String], limit: usize) -> Vec<String> {
    let budget = (wanted.chars().count() / 3).max(1);
    let mut scored: Vec<(usize, &String)> = candidates
        .iter()
        .filter_map(|c| {
            let d = edit_distance(wanted, c);
            (d <= budget).then_some((d, c))
        })
        .collect();
    scored.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(b.1)));
    scored.into_iter().take(limit).map(|(_, c)| c.clone()).collect()
}

fn edit_distance(a: &str, b: &str) -> usize {
    let (a, b): (Vec<char>, Vec<char>) = (a.chars().collect(), b.chars().collect());
    let mut prev: Vec<usize> = (0..=b.len()).collect();
    let mut cur = vec![0; b.len() + 1];
    for (i, ca) in a.iter().enumerate() {
        cur[0] = i + 1;
        for (j, cb) in b.iter().enumerate() {
            let cost = usize::from(ca != cb);
            cur[j + 1] = (prev[j] + cost).min(prev[j + 1] + 1).min(cur[j] + 1);
        }
        std::mem::swap(&mut prev, &mut cur);
    }
    prev[b.len()]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Db;
    use crate::open::fixture::{tmp, write_db};

    /// A small design: a top with two datapaths, one holding a register whose
    /// body sits in a generate block, plus a net named through a task scope and
    /// one written as an escaped identifier.
    const SEED: &str = "
        INSERT INTO module(id, name, def_kind) VALUES
            (1,'top','module'), (2,'dp','module'), (3,'reg','module');
        INSERT INTO tree_node(id, parent_node_id, name, node_kind, ordinal) VALUES
            (1, NULL, 'top',   'root',     0),
            (2, 1,    'u_dp0', 'instance', 0),
            (3, 2,    'g[0]',  'generate', 0),
            (4, 3,    'u_reg', 'instance', 0),
            (5, 1,    'u_dp1', 'instance', 1);
        INSERT INTO inst(id, module_id, parent_inst_id, param_signature) VALUES
            (1, 1, NULL, ''), (2, 2, 1, ''), (4, 3, 2, ''), (5, 2, 1, '');
        INSERT INTO net(id, inst_id, scope_node_id, name, decl_kind, width, is_implicit) VALUES
            (1, 1, 1, 'clk',       'wire',     1, 0),
            (2, 4, 4, 'q',         'variable', 8, 0),
            (3, 2, 3, 'g[0].sig',  'wire',     4, 0),
            (4, 1, 1, 'bump.v',    'variable', 8, 0),
            (5, 1, 1, 'foo.bar',   'wire',     1, 0);";

    fn opened(tag: &str) -> Db {
        let dir = tmp(tag);
        let path = dir.join("design.db");
        write_db(&path, crate::SCHEMA_VERSION, &[SEED]);
        Db::open(&path).unwrap()
    }

    fn resolved(db: &Db, anchor: &Anchor, path: &str) -> ResolvedSignal {
        match resolve(db.conn(), anchor, path).unwrap() {
            Ok(found) => found,
            Err(u) => panic!("{path} did not resolve: stopped at {}", u.failing_segment),
        }
    }

    fn plain(db: &Db) -> Anchor {
        anchor(db.conn(), None, None).unwrap()
    }

    #[test]
    fn segments_split_on_either_separator_and_skip_a_leading_one() {
        assert_eq!(segments("tb.u_dut.q"), ["tb", "u_dut", "q"]);
        assert_eq!(segments("/top/u_dp0"), ["top", "u_dp0"]);
        assert_eq!(segments("").len(), 0);
    }

    #[test]
    fn a_single_top_needs_no_naming_and_several_do() {
        let db = opened("anchor");
        assert_eq!(plain(&db).root_name, "top");

        let e = anchor(db.conn(), Some("nope"), None).unwrap_err();
        assert!(e.contains("no top named 'nope'"), "{e}");
        assert!(e.contains("top"), "the error lists what there is: {e}");
    }

    #[test]
    fn a_net_resolves_with_the_spine_below_the_root() {
        let db = opened("resolve");
        let a = plain(&db);

        for path in ["top.u_dp0.g[0].u_reg.q", "u_dp0.g[0].u_reg.q"] {
            let found = resolved(&db, &a, path);
            assert_eq!(found.net.net_id, 2);
            assert_eq!(found.inst, 4);
            assert_eq!(found.below_root, ["u_dp0", "g[0]", "u_reg"]);
            assert_eq!(found.local, "q");
            assert_eq!(found.path("top", '.'), "top.u_dp0.g[0].u_reg.q");
        }
    }

    #[test]
    fn a_generate_level_folds_into_the_name_it_declares() {
        let db = opened("generate");
        let a = plain(&db);
        // `g[0]` is a level of the path and not of the hierarchy: the net
        // belongs to u_dp0, under a name that carries the segment.
        let found = resolved(&db, &a, "top.u_dp0.g[0].sig");
        assert_eq!(found.net.net_id, 3);
        assert_eq!(found.inst, 2);
        assert_eq!(found.below_root, ["u_dp0"]);
        assert_eq!(found.local, "g[0].sig");
    }

    #[test]
    fn a_subroutine_scope_joins_the_name_because_the_tree_has_no_level_for_it() {
        let db = opened("task");
        let found = resolved(&db, &plain(&db), "top.bump.v");
        assert_eq!(found.net.net_id, 4);
        assert_eq!(found.inst, 1);
        assert_eq!(found.local, "bump.v");
    }

    #[test]
    fn an_escaped_identifier_holding_a_separator_is_matched_whole() {
        let db = opened("escaped");
        let a = plain(&db);
        // The name contains the separator, so no split of the text says which
        // part is a scope. Both spellings reach it.
        assert_eq!(resolved(&db, &a, "top.foo.bar").net.net_id, 5);
        assert_eq!(resolved(&db, &a, r"top.\foo.bar ").net.net_id, 5);
    }

    #[test]
    fn a_name_that_does_not_resolve_comes_back_with_the_level_that_does() {
        let db = opened("unresolved");
        let a = plain(&db);

        let Err(u) = resolve(db.conn(), &a, "top.u_dp0.g[0].u_reg.qq").unwrap() else {
            panic!("'qq' resolved")
        };
        assert_eq!(u.failing_segment, "qq");
        assert!(u.candidates.contains(&"q".to_string()));
        assert_eq!(close_matches("qq", &u.candidates, 5), ["q"]);

        let Err(u) = resolve(db.conn(), &a, "top.u_dpX.q").unwrap() else {
            panic!("'u_dpX' resolved")
        };
        assert_eq!(u.valid_prefix, Vec::<String>::new());
        assert_eq!(u.failing_segment, "u_dpX");
        assert!(u.candidates.contains(&"u_dp0".to_string()));
        assert_eq!(close_matches("u_dpX", &u.candidates, 5), ["u_dp0", "u_dp1"]);
    }

    #[test]
    fn a_prefix_is_stripped_only_at_a_segment_boundary() {
        let db = opened("prefix");
        let a = anchor(db.conn(), None, Some("tb.u_dut")).unwrap();

        let found = resolved(&db, &a, "tb.u_dut.top.u_dp0.g[0].u_reg.q");
        assert_eq!(found.net.net_id, 2);

        // A scope whose name merely begins with the prefix is a different
        // scope, and taking it would answer about the wrong design.
        let e = resolve(db.conn(), &a, "tb.u_dutch.q").unwrap_err();
        assert!(e.contains("is not under 'tb.u_dut'"), "{e}");
    }
}
