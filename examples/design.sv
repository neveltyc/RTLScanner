// Small enough to read whole, wide enough that every command has something to
// say about it: a flop behind a port, a case with a default the arms overwrite,
// a generate block, and a signal assembled from two disjoint slices.
module alu(input logic [7:0] a, b, output logic [7:0] r);
  assign r = a + b;
endmodule

module core(input logic clk, en, input logic [7:0] a, b, output logic [7:0] out);
  logic [7:0] sum;
  alu u_alu (.a(a), .b(b), .r(sum));
  always_ff @(posedge clk) begin
    if (en) out <= sum;
    else    out <= 8'h00;
  end
endmodule

module top(input logic clk, en, input logic [1:0] sel, input logic [7:0] a, b,
           output logic [7:0] out, muxed, packed_up);
  core u_core (.clk(clk), .en(en), .a(a), .b(b), .out(out));

  always_comb begin
    muxed = 8'hFF;                 // a default the arms below overwrite
    case (sel)
      2'b01: muxed = a;
      2'b10: muxed = b;
    endcase
  end

  assign packed_up[7:4] = a[3:0];  // two disjoint windows, one signal
  assign packed_up[3:0] = b[7:4];

  for (genvar i = 0; i < 2; i++) begin : lane
    logic [7:0] copy;
    assign copy = a;
  end
endmodule
