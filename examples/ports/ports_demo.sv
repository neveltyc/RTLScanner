// Demo for port connection checks: exercises a mix of widths, directions,
// unconnected ports, and width-mismatched connections.

module fifo #(
    parameter int WIDTH = 8,
    parameter int DEPTH = 16
) (
    input  logic              clk,
    input  logic              rst_n,
    input  logic              wr_en,
    input  logic [WIDTH-1:0]  wr_data,
    input  logic              rd_en,
    output logic [WIDTH-1:0]  rd_data,
    output logic              full,
    output logic              empty,
    output logic [3:0]        count    // intentionally 4 bits
);
endmodule


module alu (
    input  logic [31:0] a,
    input  logic [31:0] b,
    input  logic [3:0]  op,
    output logic [31:0] y,
    output logic        zero
);
endmodule


module top (
    input  logic        clk,
    input  logic        rst_n,
    input  logic [31:0] din,
    output logic [31:0] dout
);
    logic [31:0] alu_y;
    logic [7:0]  narrow;
    logic        full_w;

    // Correct connections
    alu u_alu (.a(din), .b(32'h1), .op(4'h0), .y(alu_y), .zero());

    // Width mismatch: alu_y is 32 bits but wr_data is only 8
    fifo #(.WIDTH(8)) u_fifo (
        .clk    (clk),
        .rst_n  (rst_n),
        .wr_en  (1'b1),
        .wr_data(alu_y),       // 32 -> 8: width mismatch
        .rd_en  (1'b0),
        .rd_data(narrow),
        .full   (full_w),
        .empty  (),            // OK: discarded output
        .count  ()             // OK: discarded output
    );

    assign dout = alu_y;
endmodule
