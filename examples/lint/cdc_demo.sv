// CDC demo — exercises rtl-lint --cdc.
//
// `cdc_violation` writes 'src_data' in clkA and reads it directly in
// clkB. `cdc_safe_2ff` writes in clkA then crosses through a 2-flop
// synchronizer chain in clkB (the chain itself is also a crossing —
// the first flop reads a clkA signal — but it is the standard pattern
// and is flagged so a reviewer can confirm intent).
//
// `single_clock` is clean: one clock domain, async reset only.

module cdc_violation (
    input  logic clka,
    input  logic clkb,
    input  logic rst_n,
    input  logic d,
    output logic q
);
    logic src_data;
    // clkA domain
    always_ff @(posedge clka or negedge rst_n)
        if (!rst_n) src_data <= 1'b0;
        else        src_data <= d;
    // clkB domain — reads src_data directly: UNSAFE crossing
    always_ff @(posedge clkb or negedge rst_n)
        if (!rst_n) q <= 1'b0;
        else        q <= src_data;
endmodule


module cdc_safe_2ff (
    input  logic clka,
    input  logic clkb,
    input  logic rst_n,
    input  logic d,
    output logic q
);
    logic src_data, meta, sync;
    always_ff @(posedge clka or negedge rst_n)
        if (!rst_n) src_data <= 1'b0;
        else        src_data <= d;
    // Standard 2-flop synchronizer. The first flop still crosses
    // clkA→clkB; rtl-lint flags it so a reviewer can verify intent.
    always_ff @(posedge clkb or negedge rst_n)
        if (!rst_n) begin
            meta <= 1'b0;
            sync <= 1'b0;
        end else begin
            meta <= src_data;
            sync <= meta;
        end
    assign q = sync;
endmodule


module single_clock (
    input  logic clk,
    input  logic rst_n,
    input  logic [7:0] d,
    output logic [7:0] q
);
    logic [7:0] r1, r2;
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) {r1, r2} <= '0;
        else        {r1, r2} <= {d, r1};
    assign q = r2;
endmodule
