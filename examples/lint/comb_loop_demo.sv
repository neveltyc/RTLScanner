// Combinational-loop demo — exercises `rtl-lint --rules comb-loop`.
//
// `comb_loop` has a two-gate combinational cycle (a -> b -> a) with no
// register in the path: an unstable design that never settles in
// simulation/synthesis. It is flagged.
//
// `registered_feedback` has the same feedback shape but a flip-flop in
// the loop, so it is legitimate sequential feedback and is NOT flagged —
// the clocked edge breaks the combinational cycle.
//
// `cross_hier_loop` closes a combinational loop *through* a child
// instance's ports, which the graph-based check still catches.

module comb_loop (
    input  logic en,
    output logic y
);
    logic a, b;
    assign a = b & en;   // a depends on b
    assign b = a | en;   // b depends on a  -> combinational loop
    assign y = a;
endmodule


module registered_feedback (
    input  logic clk,
    input  logic en,
    output logic y
);
    logic a, b;
    always_ff @(posedge clk) a <= b & en;  // register breaks the cycle
    assign b = a | en;
    assign y = a;
endmodule


module inv (input logic i, output logic o);
    assign o = ~i;
endmodule

module cross_hier_loop (output logic y);
    logic a, b;
    inv u1 (.i(a), .o(b));   // b = ~a
    inv u2 (.i(b), .o(a));   // a = ~b  -> loop through child ports
    assign y = a;
endmodule


// `const_dead_branch` has NO real combinational loop: the only edge that would
// close a y -> z -> y cycle (`z = y`) lives in a branch guarded by a constant-0
// condition, so it is dead in elaborated hardware.  Constant-condition pruning
// (the same pass fanin/fanout/trace use) drops that dead edge, so the loop check
// must NOT flag this module.  Without pruning a phantom y -> z -> y loop appears.
module const_dead_branch (
    input  logic a,
    output logic y,
    output logic z
);
    localparam bit C = 1'b0;   // resolved constant 0 after elaboration
    assign y = z & a;          // y depends on z (live)
    always_comb begin
        if (C) z = y;          // dead branch: would add the phantom z <- y edge
        else   z = a;          // live: z <- a
    end
endmodule
