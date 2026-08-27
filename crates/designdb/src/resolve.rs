//! Turning a hierarchical path into the net it names.
//!
//! Two things a path is not. It is not a hierarchy: a generate block is a level
//! of the path but not a level of the instance tree, and a net declared inside
//! one belongs to the enclosing instance under a name that carries the generate
//! segments. And its separators are not its structure: an escaped identifier
//! may hold a `.`, so the text is first split into the LEVELS it names — see
//! [`levels`] — and every lookup, at any depth, matches a whole level. Nothing
//! here trusts a `.` to mean a boundary.

use crate::err;
use crate::schema::{self, NetRow};

/// Separators a path may be written with. `/` is what some waveform tools
/// spell a hierarchy with, and a caller pasting one in should not have to
/// translate it first.
const SEPARATORS: [char; 2] = ['.', '/'];

/// Split a path into segments, skipping a leading separator. A segment is not
/// a level of the path — see [`levels`], which is what a lookup takes.
fn segments(path: &str) -> Vec<&str> {
    path.split(SEPARATORS).filter(|s| !s.is_empty()).collect()
}

/// Split a path into the levels it names, which is not the same as its
/// segments: an escaped identifier may hold a separator, and LRM 5.6.1 ends
/// such a name at whitespace. `\u.1 .v` has three segments and two levels,
/// `\u.1 ` and `v`. Looking a level up one segment at a time would never find
/// it, which is the whole reason a path is assembled rather than split.
///
/// Joining levels back with `.` restores the text, so a caller may still take
/// the leaf as everything from some level onward.
pub fn levels(path: &str) -> Vec<String> {
    let segments = segments(path);
    let mut levels = Vec::with_capacity(segments.len());
    let mut at = 0;
    while at < segments.len() {
        let span = match segments[at] {
            open if open.starts_with('\\') && !open.ends_with(char::is_whitespace) => segments[at..]
                .iter()
                .position(|s| s.ends_with(char::is_whitespace))
                .map_or(1, |i| i + 1),
            _ => 1,
        };
        levels.push(segments[at..at + span].join("."));
        at += span;
    }
    levels
}

/// Where the design sits in the coordinates a path is written in.
///
/// The database is rooted at the module the export made its top. A waveform is
/// rooted at the testbench that ran the simulation, where this design is one
/// instance among others — so the same net is `tb.u_dut.u_core.q` there and
/// `top.u_core.q` here. Two things differ, not one: the levels above are in
/// neither world, and the level itself is spelled with the instance's name
/// there and the module's here.
///
/// The caller may state where the root sits; where it does not, the levels
/// above are inferred, because which ones they are is a hypothesis the design
/// can test rather than a fact only the caller holds.
///
/// Which of several instances of this design a waveform meant is not a
/// question these rows can answer at all: the export elaborated the module
/// once, so every instance of it in a testbench maps to the same rows. What
/// the levels above are worth is telling the caller which ones were dropped.
#[derive(Debug, Clone)]
pub struct Anchor {
    /// The root instance's node id.
    pub root: i64,
    /// The root's own name, as the design spells it.
    pub root_name: String,
    /// Where the root sits in the caller's coordinates, without a trailing
    /// separator. Empty means it was not stated, and the levels above a path
    /// are worked out from the path.
    pub stated: String,
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
    /// The levels of the asked-for path that were above the design and
    /// discarded, joined with `.`. Reported, because a path reinterpreted
    /// without saying so is a path the caller cannot check.
    pub discarded: Option<String>,
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
    stated: Option<&str>,
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
        stated: stated.unwrap_or("").trim_end_matches(SEPARATORS).to_string(),
    })
}

/// How many of a path's leading levels are the stated anchor.
///
/// Zero where the path does not carry it: a stated anchor says where the root
/// sits in the caller's coordinates, not which coordinates every path must be
/// written in. Requiring it made `path FROM TO` demand that both operands be
/// spelled alike, for no gain — a path already relative to the design resolves
/// on its own.
///
/// The match is by level, so `tb.u_dut` does not match the front of
/// `tb.u_dutch`, and either separator matches either: the path this strips a
/// testbench off is usually a waveform tool's, and the two spellings name one
/// hierarchy.
fn stated_levels(anchor: &Anchor, levels: &[String]) -> usize {
    if anchor.stated.is_empty() {
        return 0;
    }
    let stated = self::levels(&anchor.stated);
    match levels.len() > stated.len() && levels[..stated.len()] == stated[..] {
        true => stated.len(),
        false => 0,
    }
}

/// How many of a path's leading levels are above the design altogether.
///
/// A waveform's path is anchored at the testbench, and the levels between it
/// and this design are in neither world — the export never elaborated them.
/// Which ones they are is a hypothesis the design can test, so it is tested
/// rather than demanded: levels are discarded while none of them names
/// anything at the root.
///
/// The first that does ends it, even if what follows fails. A path that
/// reaches the design and then goes wrong inside it is a mistake in the path,
/// not a different set of coordinates, and reading on would discard the
/// caller's own scopes until some name matched further down — answering
/// confidently about a net they never asked for.
/// `None` where no level of the path names anything here at all — the path is
/// not this design's, and the level that failed is not a name to correct.
fn above_the_design(
    c: &rusqlite::Connection,
    anchor: &Anchor,
    levels: &[String],
) -> Result<Option<usize>, String> {
    let root_inst = schema::node(c, anchor.root)?.and_then(|n| n.inst_id);
    for (i, level) in levels.iter().enumerate() {
        if *level == anchor.root_name || schema::child_node(c, anchor.root, level)?.is_some() {
            return Ok(Some(i));
        }
        // The root's own ports are levels too, and the last one. They are what
        // a waveform points at most often, so a path ending in one has to be
        // recognised as reaching the design.
        if i + 1 == levels.len()
            && let Some(inst) = root_inst
            && schema::net_by_name(c, inst, level)?.is_some()
        {
            return Ok(Some(i));
        }
    }
    Ok(None)
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

/// Walk `levels` down from `root` and resolve `leaf` as a net there.
///
/// The two cursors are the point: `node` descends every level of the path,
/// while `inst` only advances at an instance. Generate levels fold into the
/// name instead, which is where a net declared in one actually lives.
fn walk(
    c: &rusqlite::Connection,
    root: i64,
    levels: &[String],
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

    let mut rest = levels;
    while let Some((level, tail)) = rest.split_first() {
        match schema::child_node(c, node, level)? {
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
                match detour(c, root, node, inst, level)? {
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
    local.extend(rest.iter().cloned());
    local.push(leaf.to_string());
    let local = local.join(".");

    if let Some(net) = schema::net_by_name(c, inst, &local)? {
        return Ok(Some(ResolvedSignal { inst, below_root: below, local, net, discarded: None }));
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
                discarded: None,
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
/// `None` is no detour. `Some(None)` is one with nothing behind it — a
/// synthesized interface shape the export could put no occurrence against.
/// `Some(Some(node))` is where to carry on, which for a modport is the node
/// already there: a modport views an interface's nets without holding any, so
/// `b.mst.vld` and `b.vld` name one net and only the second is its name.
fn detour(
    c: &rusqlite::Connection,
    root: i64,
    node: i64,
    inst: i64,
    segment: &str,
) -> Result<Option<Option<i64>>, String> {
    // An interface port is a scope in a path but a terminal in the database,
    // so a member reference through one goes to the interface it is bound to.
    if let Some(term) = schema::interface_terminal(c, inst, segment)? {
        let bound = schema::iface_targets(c, term.term_id)?;
        // An array port binds one interface per element. Which of them
        // `q.vld` means is not in the path, and answering about the first
        // would name `barr[0]`'s net under a name that does not say so.
        if bound.len() > 1 {
            let mut named: Vec<String> = Vec::new();
            for target in bound.iter().flatten() {
                if let Some(spine) = path_below_root(c, root, *target)? {
                    named.push(spine.join("."));
                }
            }
            return Err(err(format!(
                "'{segment}' binds {} interfaces ({}); name the one you mean",
                bound.len(),
                named.join(", ")
            )));
        }
        return Ok(Some(bound.into_iter().next().flatten()));
    }
    Ok(schema::names_modport(c, inst, segment)?.then_some(Some(node)))
}

/// What a path did not resolve to, with enough of the design to correct it.
#[derive(Debug, Clone)]
pub struct Unresolved {
    /// The longest prefix of the path that named something.
    pub valid_prefix: Vec<String>,
    /// The level that did not — one level, which an escaped identifier may
    /// spell with separators in it.
    pub failing_segment: String,
    /// What that level does contain.
    pub candidates: Vec<String>,
    /// No level of the path named anything here. The failing level is then not
    /// a name to correct but a level to drop, and a message that offers a
    /// spelling correction sends the caller to fix the wrong word.
    pub anchored_elsewhere: bool,
}

/// The levels of a path that are the design's, and the ones above it that
/// were not — a testbench, a wrapper, whatever else ran the simulation.
///
/// Every command that takes a path goes through here, so a scope and a signal
/// are read in the same coordinates.
pub fn below_the_anchor(
    c: &rusqlite::Connection,
    anchor: &Anchor,
    path: &str,
) -> Result<(Vec<String>, Option<String>, bool), String> {
    let mut levels = levels(path);
    // Where the root sits in the caller's coordinates: taken from the anchor
    // where it was stated and the path carries it, worked out from the path
    // where it was not.
    let (above, reached) = match stated_levels(anchor, &levels) {
        // Nothing in the path names anything here. Discarding all of it would
        // leave nothing to report, so it is read whole and the failure says
        // that no part of it reached the design.
        0 => match above_the_design(c, anchor, &levels)? {
            Some(above) => (above, true),
            None => (0, false),
        },
        stated => (stated, true),
    };
    let discarded = (above > 0).then(|| levels[..above].join("."));
    levels.drain(..above);
    // A path may or may not repeat the root's own name; both spell the same
    // net, and the design is what says which one this is.
    if levels.first().map(String::as_str) == Some(anchor.root_name.as_str()) {
        levels.remove(0);
    }
    Ok((levels, discarded, reached))
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
    let (levels, discarded, reached) = below_the_anchor(c, anchor, path)?;
    if levels.is_empty() {
        return Ok(Err(Unresolved {
            valid_prefix: Vec::new(),
            failing_segment: path.to_string(),
            candidates: names_under(c, anchor.root)?,
            anchored_elsewhere: !reached,
        }));
    }

    for split in (0..levels.len()).rev() {
        let leaf = levels[split..].join(".");
        if let Some(mut found) = walk(c, anchor.root, &levels[..split], &leaf)? {
            found.discarded = discarded;
            return Ok(Ok(found));
        }
    }
    let mut why = diagnose(c, anchor, &levels)?;
    why.anchored_elsewhere = !reached;
    Ok(Err(why))
}

/// Walk as far as the path does resolve, and report what is at the level it
/// stopped in. A caller correcting a name needs the level, not the failure.
fn diagnose(
    c: &rusqlite::Connection,
    anchor: &Anchor,
    levels: &[String],
) -> Result<Unresolved, String> {
    let mut node = anchor.root;
    let mut inst = schema::node(c, anchor.root)?.and_then(|n| n.inst_id);
    let mut valid = Vec::new();

    for (i, level) in levels.iter().enumerate() {
        // The last level is the name, not a scope: a path failing there failed
        // to name a net, and the level above it is what to list.
        if i + 1 == levels.len() {
            break;
        }
        let crossed = match schema::child_node(c, node, level)? {
            Some(child) => Some((child.node_id, child.inst_id)),
            // A generate level has no instance, and `walk` takes no detour
            // from one either.
            None => match inst {
                None => None,
                Some(here) => match detour(c, anchor.root, node, here, level)? {
                    Some(Some(next)) => Some((next, Some(next))),
                    _ => None,
                },
            },
        };
        match crossed {
            Some((next_node, next_inst)) => {
                node = next_node;
                inst = next_inst;
                valid.push(level.clone());
            }
            None => {
                return Ok(Unresolved {
                    valid_prefix: valid,
                    failing_segment: level.clone(),
                    candidates: names_under(c, node)?,
                    anchored_elsewhere: false,
                });
            }
        }
    }
    Ok(Unresolved {
        valid_prefix: valid,
        failing_segment: levels.last().cloned().unwrap_or_default(),
        candidates: names_under(c, node)?,
        anchored_elsewhere: false,
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
            (1, NULL, 'top',    'root',     0),
            (2, 1,    'u_dp0',  'instance', 0),
            (3, 2,    'g[0]',   'generate', 0),
            (4, 3,    'u_reg',  'instance', 0),
            (5, 1,    'u_dp1',  'instance', 1),
            (6, 1,    '\\u.1 ', 'instance', 2);
        INSERT INTO inst(id, module_id, parent_inst_id, param_signature) VALUES
            (1, 1, NULL, ''), (2, 2, 1, ''), (4, 3, 2, ''), (5, 2, 1, ''), (6, 3, 1, '');
        INSERT INTO net(id, inst_id, scope_node_id, name, decl_kind, width, is_implicit) VALUES
            (1, 1, 1, 'clk',       'wire',     1, 0),
            (2, 4, 4, 'q',         'variable', 8, 0),
            (3, 2, 3, 'g[0].sig',  'wire',     4, 0),
            (4, 1, 1, 'bump.v',    'variable', 8, 0),
            (5, 1, 1, 'foo.bar',   'wire',     1, 0),
            (6, 6, 6, 'v',         'variable', 8, 0);";

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
    fn an_escaped_identifier_holding_a_separator_is_a_scope_too() {
        // The leaf was already matched whole; a level of the path is the same
        // name in the same spelling, and looking it up a segment at a time
        // reaches an instance called `\u` that is not there.
        let db = opened("escaped-scope");
        let a = plain(&db);
        let found = resolved(&db, &a, r"top.\u.1 .v");
        assert_eq!(found.net.net_id, 6);
        assert_eq!(found.inst, 6);
        assert_eq!(found.below_root, [r"\u.1 "]);
        assert_eq!(found.path("top", '.'), r"top.\u.1 .v");

        // And the level is what a failure below it reports, so a correction
        // starts from a name that exists.
        let Err(u) = resolve(db.conn(), &a, r"top.\u.1 .vv").unwrap() else { panic!("resolved") };
        assert_eq!(u.valid_prefix, [r"\u.1 "]);
        assert_eq!(u.failing_segment, "vv");
    }

    #[test]
    fn levels_end_an_escaped_identifier_at_its_terminator() {
        assert_eq!(levels(r"top.\u.1 .v"), ["top", r"\u.1 ", "v"]);
        // Already terminated by the first segment, so it stands alone.
        assert_eq!(levels(r"top.\u[2] .v"), ["top", r"\u[2] ", "v"]);
        // No terminator anywhere: nothing says where the name ends, so the
        // path is read as written rather than swallowing the rest of it.
        assert_eq!(levels(r"top.\u.1.v"), ["top", r"\u", "1", "v"]);
        assert_eq!(levels("top.u_dp0.q"), ["top", "u_dp0", "q"]);
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
    fn a_stated_anchor_matches_whole_levels_and_either_separator() {
        let db = opened("anchor-stated");
        for stated in ["tb.u_dut", "tb/u_dut"] {
            let a = anchor(db.conn(), None, Some(stated)).unwrap();
            for path in ["tb.u_dut.top.u_dp0.g[0].u_reg.q", "tb/u_dut/top/u_dp0/g[0]/u_reg/q"] {
                let found = resolved(&db, &a, path);
                assert_eq!(found.net.net_id, 2, "{stated} / {path}");
                assert_eq!(found.discarded.as_deref(), Some("tb.u_dut"), "{stated} / {path}");
            }
        }

        // A level whose name merely begins with it is a different level, so
        // nothing is taken off the front of it. What is left is then read like
        // any other foreign path — the whole of `tb.u_dutch`, never `ch`.
        let a = anchor(db.conn(), None, Some("tb.u_dut")).unwrap();
        let found = resolved(&db, &a, "tb.u_dutch.clk");
        assert_eq!(found.net.net_id, 1);
        assert_eq!(found.discarded.as_deref(), Some("tb.u_dutch"));
    }

    #[test]
    fn a_stated_anchor_is_an_override_and_not_a_requirement() {
        // It says where the root sits in the caller's coordinates, not which
        // coordinates every path has to be written in. Demanding the second
        // made a two-path question insist both operands be spelled alike.
        let db = opened("anchor-override");
        let a = anchor(db.conn(), None, Some("tb.u_dut")).unwrap();
        let found = resolved(&db, &a, "top.u_dp0.g[0].u_reg.q");
        assert_eq!(found.net.net_id, 2);
        assert_eq!(found.discarded, None);
    }

    #[test]
    fn a_path_from_another_world_finds_the_design_on_its_own() {
        // A waveform's path is anchored at the testbench that ran the
        // simulation. Which levels those are is a hypothesis this design can
        // test, so it is tested rather than demanded.
        let db = opened("anchor-inferred");
        let a = plain(&db);
        for (path, discarded) in [
            ("tb.u_dut.u_dp0.g[0].u_reg.q", Some("tb.u_dut")),
            ("tb.dut.wrap.i_top.u_dp0.g[0].u_reg.q", Some("tb.dut.wrap.i_top")),
            ("tb.u_dut.top.u_dp0.g[0].u_reg.q", Some("tb.u_dut")),
            ("top.u_dp0.g[0].u_reg.q", None),
            ("u_dp0.g[0].u_reg.q", None),
        ] {
            let found = resolved(&db, &a, path);
            assert_eq!(found.net.net_id, 2, "{path}");
            assert_eq!(found.discarded.as_deref(), discarded, "{path}");
        }
        // A port of the root is a level too, and the one a waveform points at
        // most often.
        assert_eq!(resolved(&db, &a, "tb.u_dut.clk").net.net_id, 1);
    }

    #[test]
    fn a_path_that_reaches_the_design_and_then_fails_is_not_read_on() {
        // `u_dp0` names something here, so the caller is in these coordinates
        // and `nope` is their mistake. Discarding it to keep looking would find
        // `clk` at the root and answer confidently about a net nobody asked
        // for.
        let db = opened("anchor-stops");
        let a = plain(&db);
        let Err(u) = resolve(db.conn(), &a, "tb.u_dut.u_dp0.nope.clk").unwrap() else {
            panic!("read past the design's own level")
        };
        assert_eq!(u.valid_prefix, ["u_dp0"]);
        assert_eq!(u.failing_segment, "nope");
    }
}
