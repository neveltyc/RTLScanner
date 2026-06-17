// Bit-level dataflow demo.  Exercises precise bit correspondence (selects,
// truncation, concatenation) and the conservative fallback (arithmetic).
module bits_top (
    input  logic [31:0] din,
    input  logic [7:0]  a, b,
    output logic [3:0]  narrow,
    output logic [7:0]  dout,
    output logic [7:0]  sum
);
    assign narrow   = din;        // truncation: narrow[3:0] <- din[3:0]
    assign dout[5]  = a[2];       // single-bit rename: dout[5] <- a[2]
    assign dout[3:0]= a[7:4];     // range shift:       dout[3:0] <- a[7:4]
    assign dout[7:6]= b[1:0];     // range shift:       dout[7:6] <- b[1:0]
    assign sum      = a + b;      // arithmetic: conservative, whole signals
endmodule
