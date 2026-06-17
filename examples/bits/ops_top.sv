// Structural bit-flow: concatenation, mux (?:), and bitwise ops resolve to
// precise bit ranges (slang-netlist parity), not whole-signal connections.
module ops_top (
    input  logic [7:0]  a, b,
    input  logic        sel,
    output logic [7:0]  cat8,     // {a[3:0], b[3:0]}: cat8[7:4]<-a[3:0], [3:0]<-b
    output logic [15:0] cat16,    // {a, b}: cat16[15:8]<-a, cat16[7:0]<-b
    output logic [7:0]  muxed,    // sel ? a : b: per-bit a/b; sel whole
    output logic [3:0]  masked    // a[7:4] & b[3:0]: bitwise of two slices
);
    assign cat8   = {a[3:0], b[3:0]};
    assign cat16  = {a, b};
    assign muxed  = sel ? a : b;
    assign masked = a[7:4] & b[3:0];
endmodule
