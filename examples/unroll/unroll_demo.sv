// Constant-condition pruning / procedural loop unrolling demo (slang-netlist
// alignment).  Compare any query with `--no-unroll` to see the conservative
// (symbol-level) result the prune/unroll pass refines:
//
//   rtlscanner fanin -d examples/unroll -s q   --scope prune  --no-unroll
//   rtlscanner fanin -d examples/unroll -s q   --scope prune
//   rtlscanner fanin -d examples/unroll -s hi  --scope window
//   rtlscanner fanin -d examples/unroll -s q   --scope loop_prune

// Constant if / case selectors: the dead branch never contributes an edge.
module prune (
    input  logic [3:0] a, b, c, d,
    output logic [3:0] q
);
    localparam bit      EN  = 1'b0;   // resolved constant after elaboration
    localparam int      SEL = 2;
    always_comb begin
        q = a;
        if (EN) q = b;                // dead: EN is constant 0 -> b dropped
        else    q = c;                // live
        case (SEL)
            0: q = a;
            1: q = b;
            2: q = d;                 // taken -> only d kept from the case
            default: q = a;
        endcase
    end
endmodule

// Windowed loop: each iteration reads a distinct, statically-offset slice, so
// unrolling recovers the exact bit correspondence (hi <- a[3:2]) that a dynamic
// index `a[i+2]` would otherwise blur to the whole signal.
module window (
    input  logic [3:0] a,
    output logic [1:0] hi
);
    always_comb
        for (int i = 0; i < 2; i++)
            hi[i] = a[i + 2];         // hi[0]<-a[2], hi[1]<-a[3]
endmodule

// Bit reversal: each iteration maps one source bit to a different target bit
// with a *different* offset, so the map is a permutation no single affine
// offset can express.  Unrolling keeps the exact per-bit correspondence
// (`fanout din` shows din[7]->rev[0], din[6]->rev[1], …) instead of blurring
// to a whole-signal din -> rev edge.
//   rtlscanner fanout -d examples/unroll -s din --scope reverse
module reverse (
    input  logic [7:0] din,
    output logic [7:0] rev
);
    always_comb
        for (int i = 0; i < 8; i++)
            rev[i] = din[7 - i];      // rev[0]<-din[7], rev[1]<-din[6], …
endmodule

// Per-iteration pruning: with the loop variable bound, the inner predicate
// folds each iteration, so only the live assignment survives.
module loop_prune (
    input  logic [3:0] d0, d1, d2, d3,
    output logic [3:0] q
);
    always_comb begin
        q = '0;
        for (int i = 0; i < 4; i++)
            if (i == 2) q = d2;       // only i==2 contributes -> q <- d2
    end
endmodule
