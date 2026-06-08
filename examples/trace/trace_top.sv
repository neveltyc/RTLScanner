module mux2 #(parameter int W = 8) (
    input  logic [W-1:0] a, b,
    input  logic         sel,
    output logic [W-1:0] y
);
    assign y = sel ? b : a;
endmodule

module pipe_reg #(parameter int W = 8) (
    input  logic         clk, rst_n, en,
    input  logic [W-1:0] d,
    output logic [W-1:0] q
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n)     q <= '0;
        else if (en)    q <= d;
endmodule

module counter #(parameter int W = 8) (
    input  logic         clk, rst_n, en,
    output logic [W-1:0] count
);
    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n)     count <= '0;
        else if (en)    count <= count + 1'b1;
endmodule

module datapath_v2 (
    input  logic       clk, rst_n, sel, en,
    input  logic [7:0] data_a, data_b,
    output logic [7:0] result,
    output logic [7:0] count_out
);
    logic [7:0] mux_out, pipe_q, sum;
    assign sum = pipe_q + mux_out;
    mux2 #(.W(8)) u_mux  (.a(data_a), .b(data_b), .sel(sel), .y(mux_out));
    pipe_reg #(.W(8)) u_pipe (.clk(clk), .rst_n(rst_n), .en(en), .d(mux_out), .q(pipe_q));
    counter  #(.W(8)) u_cnt  (.clk(clk), .rst_n(rst_n), .en(en), .count(count_out));
    assign result = sum;
endmodule

module trace_top (
    input  logic       clk, rst_n, mode, enable,
    input  logic [7:0] in_a, in_b,
    output logic [7:0] out_result, out_count
);
    datapath_v2 u_dp (
        .clk(clk), .rst_n(rst_n), .sel(mode), .en(enable),
        .data_a(in_a), .data_b(in_b),
        .result(out_result), .count_out(out_count)
    );
endmodule
