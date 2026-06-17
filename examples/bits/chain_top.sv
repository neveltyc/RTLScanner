// Multi-hop bit-level dataflow: a nibble swap behind a module boundary.
// dout's high nibble traces back (through the swap) to `lo`, and dout's low
// nibble to `hi` — exercising range mapping across several hops + ports.
module nibble_swap (input logic [7:0] x, output logic [7:0] y);
    assign y[7:4] = x[3:0];   // swap: high nibble of y from low nibble of x
    assign y[3:0] = x[7:4];
endmodule

module chain_top (
    input  logic [3:0] lo,
    input  logic [3:0] hi,
    output logic [7:0] dout
);
    logic [7:0] mid;
    assign mid[3:0] = lo;     // mid low nibble  <- lo
    assign mid[7:4] = hi;     // mid high nibble <- hi
    nibble_swap u_sw (.x(mid), .y(dout));
endmodule
