//! Bit ranges, as the schema encodes them.
//!
//! A range is LSB-relative offsets into the flattened object, never the
//! declared indices: `logic [15:8] off` has bit 15 at offset 7. The declared
//! shape is recoverable from the type text, and [`spell`] does that — a
//! consumer that printed offsets as indices would mislabel every signal not
//! declared `[N-1:0]`.

/// What a row says about which bits it touches.
///
/// The three states are the schema's two NULL cases plus the range: "the whole
/// object" and "somewhere inside it, unknown where" are different facts and are
/// stored as different rows, so collapsing them would answer a question the
/// database took care to keep open.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum BitSpan {
    /// The whole object.
    Whole,
    /// Somewhere inside it, position not statable — a dynamic selector.
    Unknown,
    /// These bits. `exact` false makes them an upper bound, not the bits
    /// actually touched.
    Range { lo: u64, hi: u64, exact: bool },
}

impl BitSpan {
    /// Read the (lo, hi, exact) triple a row carries.
    pub fn read(lo: Option<i64>, hi: Option<i64>, exact: Option<i64>) -> BitSpan {
        match (lo, hi) {
            (Some(lo), Some(hi)) => {
                BitSpan::Range { lo: lo.max(0) as u64, hi: hi.max(0) as u64, exact: exact != Some(0) }
            }
            // NULL bits: exact tells "the whole of it" from "inside it,
            // somewhere". A row with neither is the whole object.
            _ if exact == Some(0) => BitSpan::Unknown,
            _ => BitSpan::Whole,
        }
    }

    /// Whether this span can touch `[lo, hi]`.
    ///
    /// `Unknown` can: the database says only "somewhere inside", and dropping
    /// it would answer as though it had said "not here".
    pub fn may_touch(&self, lo: u64, hi: u64) -> bool {
        match self {
            BitSpan::Whole | BitSpan::Unknown => true,
            BitSpan::Range { lo: a, hi: b, .. } => *a <= hi && lo <= *b,
        }
    }

    /// Whether two spans can touch the same bit. The uncertain states can
    /// touch anything, for the reason [`may_touch`](Self::may_touch) gives.
    pub fn overlaps(&self, other: &BitSpan) -> bool {
        match (self, other) {
            (BitSpan::Range { lo, hi, .. }, other) | (other, BitSpan::Range { lo, hi, .. }) => {
                other.may_touch(*lo, *hi)
            }
            _ => true,
        }
    }

    /// Whether a touch of this span is known bit for bit.
    pub fn is_exact(&self) -> bool {
        match self {
            BitSpan::Whole => true,
            BitSpan::Unknown => false,
            BitSpan::Range { exact, .. } => *exact,
        }
    }

    /// The span in declared indices, given the object's declared range.
    ///
    /// `None` for the whole object — a caller says nothing rather than spelling
    /// a range that adds nothing — and for a span with no declared range to
    /// anchor it, since bare offsets would read as indices and be wrong for
    /// every object not declared `[N-1:0]`.
    pub fn spell(&self, decl: Option<(i64, i64)>) -> Option<String> {
        let (BitSpan::Range { lo, hi, .. }, Some((left, right))) = (self, decl) else {
            return None;
        };
        // Offsets count from the LSB, which is whichever end of the declared
        // range is lower; the direction of the declaration is preserved.
        let (low, step) = if left >= right { (right, 1i64) } else { (right, -1i64) };
        let at = |offset: u64| low + step * offset as i64;
        let (a, b) = (at(*lo), at(*hi));
        Some(if a == b { format!("[{a}]") } else { format!("[{}:{}]", a.max(b), a.min(b)) })
    }
}

/// The declared range of a simple vector, if this type is one.
///
/// `logic [7:0]` is (7, 0); `logic [0:7]` is (0, 7) and counts the other way.
/// Bit offsets index the flattened object, so a declared range can only be
/// mapped onto them when it spans the whole of it — which is exactly what
/// `width` checks. Without that guard the first `[a:b]` of
/// `logic [3:0][7:0]`, of `logic [7:0] m [0:3]`, or of a packed struct's first
/// member would be taken for the object's own range, and every select against
/// it would name the wrong bits while looking like an answer.
///
/// The aggregates are not thereby unaddressable — they have no ONE declared
/// range, so a caller is told there is nothing to measure a select against
/// rather than measured against the wrong thing.
pub fn declared_range(data_type: &str, width: Option<i64>) -> Option<(i64, i64)> {
    let open = data_type.find('[')?;
    let close = data_type[open..].find(']')? + open;
    let (left, right) = data_type[open + 1..close].split_once(':')?;
    let (left, right): (i64, i64) = (left.trim().parse().ok()?, right.trim().parse().ok()?);

    let spans = (left - right).abs() + 1;
    (width? == spans).then_some((left, right))
}

/// Split a trailing bit-select off a leaf name: `data[7:0]` is (`data`, (7, 0)).
///
/// A non-numeric index is left alone — `mem[i]` is a name this tool cannot
/// resolve a range for, and guessing would answer about the wrong bits.
pub fn split_select(leaf: &str) -> (&str, Option<(i64, i64)>) {
    let Some(open) = leaf.rfind('[') else { return (leaf, None) };
    if !leaf.ends_with(']') {
        return (leaf, None);
    }
    let inner = &leaf[open + 1..leaf.len() - 1];
    let parsed = match inner.split_once(':') {
        Some((a, b)) => a.trim().parse().ok().zip(b.trim().parse().ok()),
        // A single index is an element select, not a declared range: `[3]` is
        // bit 3, and the two must not be confused when measuring a width.
        None => inner.trim().parse().ok().map(|i: i64| (i, i)),
    };
    match parsed {
        Some(range) => (&leaf[..open], Some(range)),
        None => (leaf, None),
    }
}

/// Carry a window across one arc, onto the far end's bits.
///
/// One rule decides it: the correspondence must be exact at every step — this
/// end's span, the mapping, and the far end's span — or the window is dropped
/// and the whole far object is taken instead. A narrower answer that might be
/// the wrong bits is worse than a wider one that certainly contains them.
///
/// `None` in and `None` out both mean the whole object.
pub fn cross(
    window: Option<(u64, u64)>,
    signal: BitSpan,
    far: BitSpan,
    map_exact: Option<bool>,
) -> Option<(u64, u64)> {
    let (lo, hi) = window?;
    if map_exact != Some(true) {
        return None;
    }
    // Where the window sits within this end's span, as an offset from its base.
    let (wlo, whi, base) = match signal {
        BitSpan::Whole => (lo, hi, 0),
        BitSpan::Range { lo: a, hi: b, exact: true } => (lo.max(a), hi.min(b), a),
        _ => return None,
    };
    if wlo > whi {
        return None;
    }
    let (off_lo, off_hi) = (wlo - base, whi - base);
    match far {
        BitSpan::Whole => Some((off_lo, off_hi)),
        BitSpan::Range { lo: c, exact: true, .. } => Some((c + off_lo, c + off_hi)),
        _ => None,
    }
}

/// Declared indices to LSB-relative offsets, against the object's own range.
pub fn offsets_of(select: (i64, i64), decl: (i64, i64)) -> Result<(u64, u64), String> {
    let (left, right) = decl;
    let low = left.min(right);
    let high = left.max(right);
    let to_offset = |i: i64| -> Result<u64, String> {
        if i < low || i > high {
            return Err(format!("bit {i} is outside the declared range [{left}:{right}]"));
        }
        Ok(if left >= right { (i - right) as u64 } else { (right - i) as u64 })
    };
    let (a, b) = (to_offset(select.0)?, to_offset(select.1)?);
    Ok((a.min(b), a.max(b)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_span_reads_its_three_states_apart() {
        // NULL bits with exact=1 is the whole object; with exact=0 it is
        // somewhere inside it. Collapsing them would lose a fact the schema
        // spent a column keeping.
        assert_eq!(BitSpan::read(None, None, Some(1)), BitSpan::Whole);
        assert_eq!(BitSpan::read(None, None, Some(0)), BitSpan::Unknown);
        assert_eq!(BitSpan::read(Some(3), Some(7), Some(1)), BitSpan::Range {
            lo: 3,
            hi: 7,
            exact: true
        });
        assert_eq!(BitSpan::read(Some(0), Some(15), Some(0)), BitSpan::Range {
            lo: 0,
            hi: 15,
            exact: false
        });
    }

    #[test]
    fn an_unknown_span_may_touch_anything() {
        assert!(BitSpan::Unknown.may_touch(5, 5));
        assert!(!BitSpan::Unknown.is_exact());
        assert!(BitSpan::Whole.may_touch(0, 0));

        let r = BitSpan::Range { lo: 4, hi: 7, exact: true };
        assert!(r.may_touch(7, 12));
        assert!(r.may_touch(0, 4));
        assert!(!r.may_touch(0, 3));
        assert!(!r.may_touch(8, 8));
    }

    #[test]
    fn offsets_are_lsb_relative_in_both_declaration_directions() {
        // Descending: bit 15 of [15:8] is the top of an eight-bit object.
        assert_eq!(offsets_of((15, 15), (15, 8)).unwrap(), (7, 7));
        assert_eq!(offsets_of((8, 8), (15, 8)).unwrap(), (0, 0));
        assert_eq!(offsets_of((10, 9), (15, 8)).unwrap(), (1, 2));
        // Ascending: [0:7] declares bit 0 as the most significant.
        assert_eq!(offsets_of((0, 0), (0, 7)).unwrap(), (7, 7));
        assert_eq!(offsets_of((7, 7), (0, 7)).unwrap(), (0, 0));
        assert_eq!(offsets_of((3, 0), (7, 0)).unwrap(), (0, 3));

        assert!(offsets_of((16, 16), (15, 8)).is_err());
        assert!(offsets_of((7, 7), (15, 8)).is_err());
    }

    #[test]
    fn a_window_crosses_only_where_every_step_is_exact() {
        let whole = BitSpan::Whole;
        let exact = |lo, hi| BitSpan::Range { lo, hi, exact: true };
        let inexact = |lo, hi| BitSpan::Range { lo, hi, exact: false };

        // A port wiring `[7:4]` of one net to the low half of another rebases
        // the window onto the far end's own offsets.
        assert_eq!(cross(Some((5, 6)), exact(4, 7), exact(0, 3), Some(true)), Some((1, 2)));
        assert_eq!(cross(Some((0, 3)), whole, exact(8, 11), Some(true)), Some((8, 11)));
        assert_eq!(cross(Some((2, 2)), whole, whole, Some(true)), Some((2, 2)));

        // Whichever step is not exact, the answer is the whole far object: a
        // sum's operands correspond to no particular bits of it, and an
        // upper-bound range is not the bits actually touched.
        assert_eq!(cross(Some((0, 3)), whole, whole, Some(false)), None, "no per-bit mapping");
        assert_eq!(cross(Some((0, 3)), whole, whole, None), None, "nothing to correspond with");
        assert_eq!(cross(Some((0, 3)), inexact(0, 7), whole, Some(true)), None);
        assert_eq!(cross(Some((0, 3)), whole, inexact(0, 7), Some(true)), None);
        assert_eq!(cross(Some((0, 3)), BitSpan::Unknown, whole, Some(true)), None);

        // A window disjoint from the arc degrades like any other uncertainty.
        // The walk never gets here — such a row is filtered by `may_touch`
        // first — and the safe answer is the wide one either way.
        assert_eq!(cross(Some((9, 9)), exact(0, 3), whole, Some(true)), None);
        // The whole object stays the whole object.
        assert_eq!(cross(None, whole, whole, Some(true)), None);
    }

    #[test]
    fn spelling_a_span_puts_it_back_in_declared_indices() {
        let decl = Some((15, 8));
        assert_eq!(BitSpan::Range { lo: 7, hi: 7, exact: true }.spell(decl).unwrap(), "[15]");
        assert_eq!(BitSpan::Range { lo: 0, hi: 3, exact: true }.spell(decl).unwrap(), "[11:8]");
        assert_eq!(BitSpan::Range { lo: 0, hi: 7, exact: true }.spell(Some((0, 7))).unwrap(), "[7:0]");

        // Nothing to say about the whole object, and nothing safe to say
        // without a declared range to anchor the offsets against.
        assert_eq!(BitSpan::Whole.spell(decl), None);
        assert_eq!(BitSpan::Range { lo: 0, hi: 3, exact: true }.spell(None), None);
    }

    #[test]
    fn only_a_simple_vector_has_a_range_bit_offsets_can_be_mapped_onto() {
        assert_eq!(declared_range("logic[7:0]", Some(8)), Some((7, 0)));
        assert_eq!(declared_range("logic [0:7]", Some(8)), Some((0, 7)));
        assert_eq!(declared_range("logic signed[15:8]", Some(8)), Some((15, 8)));

        // The first range of each of these spans a part, not the whole: taking
        // it for the object's range would answer about the wrong bits.
        assert_eq!(declared_range("logic[3:0][7:0]", Some(32)), None, "packed array");
        assert_eq!(declared_range("logic[7:0]$[0:3]", Some(32)), None, "unpacked array");
        assert_eq!(
            declared_range("struct packed{logic[3:0] p;logic[11:0] q;}anon.s$1", Some(16)),
            None,
            "the first member's range is not the struct's"
        );

        assert_eq!(declared_range("logic", Some(1)), None);
        assert_eq!(declared_range("logic[$]", Some(8)), None);
        assert_eq!(declared_range("logic[7:0]", None), None, "no width to check against");
    }

    #[test]
    fn a_select_splits_off_a_leaf_only_when_it_is_a_number() {
        assert_eq!(split_select("data[7:0]"), ("data", Some((7, 0))));
        assert_eq!(split_select("data[3]"), ("data", Some((3, 3))));
        assert_eq!(split_select("q"), ("q", None));
        // A variable index names bits this tool cannot identify, so the name
        // stays whole rather than being resolved against the wrong ones.
        assert_eq!(split_select("mem[i]"), ("mem[i]", None));
        assert_eq!(split_select("g[0].sig"), ("g[0].sig", None));
    }
}
