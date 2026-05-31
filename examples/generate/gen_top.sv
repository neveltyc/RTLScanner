module leaf (
    input  logic in,
    output logic out
);
    assign out = in;
endmodule

module mid #(parameter int N = 3) (
    input  logic         in,
    output logic [N-1:0] out
);
    leaf u_leaf (
        .in(in),
        .out()
    );

    for (genvar i = 0; i < N; i++) begin : gen_arr
        leaf u_gen_leaf (
            .in(in),
            .out(out[i])
        );
    end
endmodule

module gen_top (
    input  logic       in,
    output logic [2:0] out
);
    mid #(.N(3)) u_mid (
        .in(in),
        .out(out)
    );
endmodule
