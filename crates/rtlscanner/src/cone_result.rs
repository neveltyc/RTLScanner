//! Rendering a cone, and the truncation that keeps a large one readable.
//!
//! A cone of a clock reaches thousands of nets, so the answer is clipped by
//! default. Three rules make a clipped answer honest: the counts stay the true
//! ones, the note says how to lift the limit, and the graph stays consistent —
//! an edge is never kept whose endpoints were dropped, since a caller reading
//! `nodes` as the set of things `edges` refers to would be reading a lie.

use designdb::Direction;
use serde_json::{Value, json};

use crate::cone::{Bounds, Cone, Edge, Node};
use crate::envelope::CommandResult;

/// How many rows an answer shows unless told otherwise. Large enough for a
/// cone worth reading, small enough not to fill a terminal or a context
/// window.
pub const DEFAULT_LIMIT: usize = 200;

/// Resolve `--limit`: absent is the default, zero or less is everything.
pub fn resolve_limit(given: Option<i64>) -> usize {
    match given {
        None => DEFAULT_LIMIT,
        Some(n) if n <= 0 => usize::MAX,
        Some(n) => n as usize,
    }
}

pub struct ConeResult {
    cone: Cone,
    limit: usize,
}

impl ConeResult {
    pub fn new(cone: Cone, limit: usize) -> ConeResult {
        ConeResult { cone, limit }
    }

    /// The edges shown, and the nodes the answer refers to.
    ///
    /// Nodes follow what is shown rather than being clipped on their own: a
    /// node list clipped separately would leave edges — or `direct` — pointing
    /// at names that are not in it.
    fn shown(&self, direct: &[i64]) -> (Vec<&Edge>, Vec<&Node>) {
        let edges: Vec<&Edge> = self.cone.edges.iter().take(self.limit).collect();
        if edges.len() == self.cone.edges.len() {
            return (edges, self.cone.nodes.iter().collect());
        }
        let mut kept: Vec<i64> =
            edges.iter().flat_map(|e| [e.source, e.target]).chain(direct.iter().copied()).collect();
        kept.sort_unstable();
        kept.dedup();
        let nodes = self.cone.nodes.iter().filter(|n| kept.binary_search(&n.net).is_ok()).collect();
        (edges, nodes)
    }

    fn truncated(&self) -> bool {
        self.cone.edges.len() > self.limit
    }

    /// The nets one hop out, which is what a caller following a cone reads
    /// first, and how many there are.
    ///
    /// Counted over the whole first hop and clipped on its own, not taken from
    /// whatever the edge budget happened to leave: `--limit` is a bound on the
    /// cone, and reading it as a bound on the first hop as well made the one
    /// list a caller starts from quietly short of the design.
    fn direct(&self, net_of: &dyn Fn(i64) -> (String, Option<(i64, i64)>)) -> (Vec<i64>, usize) {
        let far = |e: &Edge| match self.cone.direction {
            Direction::Driver => e.source,
            Direction::Load => e.target,
        };
        let mut nets: Vec<i64> =
            self.cone.edges.iter().filter(|e| e.depth == 1).map(far).collect();
        nets.sort_unstable();
        nets.dedup();
        // By the name a caller reads, so which ones a clipped answer holds is
        // the same question twice running and not an artefact of edge order.
        nets.sort_by_cached_key(|net| net_of(*net).0);
        let total = nets.len();
        nets.truncate(self.limit);
        (nets, total)
    }
}

fn node_json(n: &Node) -> Value {
    json!({
        "path": n.path,
        "depth": n.depth,
        "width": n.width,
        "clocked": n.clocked,
        "latch": n.latch,
    })
}

/// One edge, with each end's bits spelled against that end's own declared
/// range — the two ends are different objects and rarely declared alike.
fn edge_json(e: &Edge, net: &dyn Fn(i64) -> (String, Option<(i64, i64)>)) -> Value {
    let (source, src_decl) = net(e.source);
    let (target, tgt_decl) = net(e.target);
    json!({
        "source": source,
        "target": target,
        "kind": e.kind.tag(),
        "raw_kind": e.raw_kind,
        "depth": e.depth,
        "boundary": e.boundary,
        "control": e.control,
        "clocked": e.clocked,
        "source_bits": designdb::bits::say(&e.src_bits, src_decl),
        "target_bits": designdb::bits::say(&e.tgt_bits, tgt_decl),
        "map_exact": e.map_exact,
        // Where a combinational walk stopped, so an empty answer can say why.
        "ends_at_state": e.ends_at_state,
        // A constant condition rules this arm out at this parameterisation:
        // the statement is in the design, the dependency is not in this build.
        "unreachable": e.unreachable,
        // Which expansion the row belongs to, where a subroutine body produced
        // it: one call's rows are not another's.
        "call_site": e.call_site_id,
        "file": e.file.as_deref(),
        "line": e.line,
    })
}

impl CommandResult for ConeResult {
    fn to_json(&self) -> (Value, Value) {
        let named: std::collections::HashMap<i64, (String, Option<(i64, i64)>)> =
            self.cone.nodes.iter().map(|n| (n.net, (n.path.clone(), n.decl))).collect();
        let net_of =
            |net: i64| named.get(&net).cloned().unwrap_or_else(|| (format!("<net {net}>"), None));
        let (direct, direct_total) = self.direct(&net_of);
        let (edges, nodes) = self.shown(&direct);

        let data = json!({
            "start": self.cone.start,
            "direction": self.cone.direction.tag(),
            "max_depth": self.cone.bounds.max_depth,
            "comb": self.cone.bounds.comb,
            "control": self.cone.bounds.gating.tag(),
            "nodes": nodes.iter().map(|n| node_json(n)).collect::<Vec<_>>(),
            "edges": edges.iter().map(|e| edge_json(e, &net_of)).collect::<Vec<_>>(),
            // The whole first hop, clipped on its own terms; `summary.direct`
            // says how many there are.
            "direct": direct.iter().map(|net| net_of(*net).0).collect::<Vec<_>>(),
        });
        let summary = json!({
            // The true counts, whatever was shown: a clipped list that also
            // clipped its own total would say the cone is the size of the
            // window onto it.
            "nodes": self.cone.nodes.len(),
            "edges": self.cone.edges.len(),
            "shown_edges": edges.len(),
            "shown_nodes": nodes.len(),
            // The first hop is its own list with its own bound: a caller
            // reading `direct` as "what feeds this" needs to know when it is
            // holding part of one.
            "direct": direct_total,
            "shown_direct": direct.len(),
            "stopped_at_state": self.cone.edges.iter().filter(|e| e.ends_at_state).count(),
            "max_depth_reached": self.cone.nodes.iter().map(|n| n.depth).max().unwrap_or(0),
            "control_edges": self.cone.edges.iter().filter(|e| e.control).count(),
            "unreachable_edges": self.cone.edges.iter().filter(|e| e.unreachable).count(),
            "widened": self.cone.widened,
            // Nets with a driver or load the export named only by a reference
            // it did not resolve: this cone is short by at least that much,
            // and `trace` on each of them names what is missing.
            "unresolved": self.cone.unresolved.len(),
            "truncated": self.truncated(),
            "limit": if self.limit == usize::MAX { 0 } else { self.limit },
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        // No first-hop list here, so no node is kept for one: the terminal view
        // prints edges and counts, and a name it never writes needs no row.
        let (edges, nodes) = self.shown(&[]);
        let named: std::collections::HashMap<i64, &Node> =
            self.cone.nodes.iter().map(|n| (n.net, n)).collect();
        let name = |net: i64| named.get(&net).map(|n| n.path.as_str()).unwrap_or("<net>");
        let decl = |net: i64| named.get(&net).and_then(|n| n.decl);

        let mut out = String::new();
        let word = match self.cone.direction {
            Direction::Driver => "fan-in",
            Direction::Load => "fan-out",
        };
        out.push_str(&format!("{word} of {}\n", self.cone.start));
        out.push_str(&format!(
            "{} node(s), {} edge(s){}{}\n\n",
            self.cone.nodes.len(),
            self.cone.edges.len(),
            if self.cone.bounds.comb { ", combinational" } else { "" },
            match self.cone.edges.iter().filter(|e| e.control).count() {
                0 => String::new(),
                n => format!(", {n} of them conditions"),
            }
        ));

        for edge in &edges {
            let at = match (&edge.file, edge.line) {
                (Some(f), Some(l)) => format!("{}:{l}", f.rsplit('/').next().unwrap_or(f)),
                _ => String::new(),
            };
            let marks = [
                edge.clocked.then_some("clocked"),
                edge.boundary.then_some("boundary"),
                edge.control.then_some("condition"),
                edge.unreachable.then_some("unreachable at this parameterisation"),
                edge.ends_at_state.then_some("stops here: state element"),
            ]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>();
            let note =
                if marks.is_empty() { String::new() } else { format!("  [{}]", marks.join(", ")) };
            // Each end's bits against its own declared range: the two are
            // different objects and rarely declared alike. `@[hi:lo]` is an
            // offset from the LSB, for an end with no single declared range.
            let spell = |span: &designdb::BitSpan, net: i64| {
                designdb::bits::say(span, decl(net)).unwrap_or_default()
            };
            out.push_str(&format!(
                "  {:>2}  {}{} -> {}{}\n      {:<18} {}{note}\n",
                edge.depth,
                name(edge.source),
                spell(&edge.src_bits, edge.source),
                name(edge.target),
                spell(&edge.tgt_bits, edge.target),
                edge.kind.tag(),
                at,
            ));
        }

        if self.truncated() {
            out.push_str(&format!(
                "\ntruncated: {}/{} edges ({} of {} nodes); --limit 0 for all\n",
                edges.len(),
                self.cone.edges.len(),
                nodes.len(),
                self.cone.nodes.len(),
            ));
        }
        if !self.cone.unresolved.is_empty() {
            out.push_str(&format!(
                "note: {} net(s) here have an arc the export named but did not resolve; \
                 this cone is short by at least that much — trace them to see it\n",
                self.cone.unresolved.len()
            ));
        }
        if self.cone.widened > 0 {
            out.push_str(&format!(
                "note: {} hop(s) lost bit precision and widened to the whole object\n",
                self.cone.widened
            ));
        }
        out
    }
}

/// A route from one net to another, or the fact that there is none.
pub struct PathResult {
    pub from: String,
    pub to: String,
    pub bounds: Bounds,
    pub route: Option<Vec<Edge>>,
    pub names: std::collections::HashMap<i64, String>,
}

impl CommandResult for PathResult {
    fn to_json(&self) -> (Value, Value) {
        let name =
            |net: i64| self.names.get(&net).cloned().unwrap_or_else(|| format!("<net {net}>"));
        let route = self.route.as_deref().unwrap_or_default();
        let net_of = |id: i64| (name(id), None);

        // The nodes a route passes through, in order: `edges[i]` joins
        // `nodes[i]` to `nodes[i+1]`, which is what makes the two readable
        // together.
        let mut nodes = vec![self.from.clone()];
        nodes.extend(route.iter().map(|e| name(e.target)));

        let data = json!({
            "from": self.from,
            "to": self.to,
            "found": self.route.is_some(),
            "comb": self.bounds.comb,
            "length": route.len(),
            "nodes": if self.route.is_some() { nodes } else { Vec::new() },
            "edges": route.iter().map(|e| edge_json(e, &net_of)).collect::<Vec<_>>(),
        });
        let summary = json!({
            "found": self.route.is_some(),
            "length": route.len(),
            "clocked_edges": route.iter().filter(|e| e.clocked).count(),
        });
        (data, summary)
    }

    fn render_human(&self) -> String {
        let name =
            |net: i64| self.names.get(&net).cloned().unwrap_or_else(|| format!("<net {net}>"));
        let mut out = format!("path: {} -> {}\n", self.from, self.to);
        let Some(route) = &self.route else {
            // Not an error: two nets with no route between them is a fact
            // about the design, and the same one every time.
            out.push_str(&format!(
                "not found{}\n",
                if self.bounds.comb { " (combinational only)" } else { "" }
            ));
            return out;
        };
        out.push_str(&format!("found, {} hop(s)\n\n", route.len()));
        out.push_str(&format!("  {}\n", self.from));
        for edge in route {
            let at = match (&edge.file, edge.line) {
                (Some(f), Some(l)) => format!("{}:{l}", f.rsplit('/').next().unwrap_or(f)),
                _ => String::new(),
            };
            let mark = if edge.clocked { "  [clocked]" } else { "" };
            out.push_str(&format!("    | {} {}{mark}\n", edge.kind.tag(), at));
            out.push_str(&format!("  {}\n", name(edge.target)));
        }
        out
    }
}
