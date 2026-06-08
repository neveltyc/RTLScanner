// Demonstration design for rtl-lint.  Intentionally contains a handful
// of classic RTL issues so the linter has something to report.

module sub (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] a,
    input  logic [7:0] b,       // never used  -> unused-port
    output logic [3:0] y
);
    logic [7:0] sum;
    logic       dead;           // never read   -> unused-variable

    assign sum = a;
    assign y   = sum;           // 8 -> 4 bits  -> width-trunc

    always_ff @(posedge clk or negedge rst_n)
        if (!rst_n) dead <= 1'b0;
        else        dead <= a[0];
endmodule

module lint_demo (
    input  logic       clk,
    input  logic       rst_n,
    input  logic [7:0] data,
    output logic [3:0] q
);
    logic [3:0] result;
    logic [1:0] mode;

    always_comb begin
        case (mode)             // no default -> case-default
            2'b00: result = data[3:0];
            2'b01: result = data[7:4];
        endcase
    end

    assign q = result;

    sub u_sub (
        .clk   (clk),
        .rst_n (rst_n),
        .a     (data),
        .b     (8'h0),
        .y     ()               // unconnected  -> empty-output-connection
    );
endmodule
