/*!
 * \file fuzz_cov.sv
 *
 * \brief
 * Fontes de feedback do fuzzer do RS5, capturadas com `bind` (o RTL não é editado).
 *
 * \detailed
 * Cada fonte é uma região de um mapa estilo AFL: um vetor de contadores de 8 bits.
 * O que cada fonte observa, como a chave vira índice e o formato dos arquivos
 * estão em riscof/fuzzer/FONTES.md. Resumo:
 *
 *   fonte 0  arestas do pipeline com duração   (fuzz_pipe_probe -> fuzz_cov_part SRC=0)
 *   fonte 1  transições de trap/CSR com contexto (fuzz_csr_probe -> fuzz_cov_part SRC=1)
 *   fonte 6  bins nomeados (índice fixo)        (fuzz_pipe_probe -> fuzz_cov_part SRC=6)
 *   fonte 7  escalares (máximo por caso)        (fuzz_pipe_probe -> fuzz_cov_scalar SRC=7)
 *   fonte 2  cobertura de linha do Verilator    (fora deste arquivo: --coverage-line)
 *
 * Plusargs (lidos por cada módulo de região):
 *   +FUZZ_COV=<prefixo>   liga a gravação; escreve <prefixo>_<SRC>.txt no $finish
 *   +FUZZ_SRC=<máscara>   bit i liga a fonte i (padrão: todas)
 *
 * Formato de <prefixo>_<SRC>.txt: uma linha por posição com contagem != 0
 *   "<índice> <contagem> <raw em hex>"      (fontes 0, 1 e 6)
 *   "<índice> <valor>"                      (fonte 7)
 * `raw` é a chave original (antes do hash) da PRIMEIRA vez que o índice foi atingido,
 * para o relatório traduzir índice em "estado A -> estado B (duração)".
 */

`include "RS5_pkg.sv"

package fuzz_cov_pkg;

    /* Finalizador do splitmix64: espalha bits parecidos antes da dobra. Obrigatório aqui,
     * diferente do AFL: dois snapshots consecutivos costumam diferir em 1 ou 2 bits e o
     * XOR direto daria chaves pequenas e amontoadas. */
    function automatic logic [63:0] mix64(input logic [63:0] x);
        logic [63:0] z;
        z = x;
        z = z ^ (z >> 30);
        z = z * 64'hbf58476d1ce4e5b9;
        z = z ^ (z >> 27);
        z = z * 64'h94d049bb133111eb;
        z = z ^ (z >> 31);
        return z;
    endfunction

    /* Buckets de contagem do AFL: 1, 2, 3, 4-7, 8-15, 16-31, 32-127, 128+ */
    function automatic logic [2:0] afl_bucket(input logic [15:0] n);
        if (n >= 128) return 3'd7;
        if (n >= 32)  return 3'd6;
        if (n >= 16)  return 3'd5;
        if (n >= 8)   return 3'd4;
        if (n >= 4)   return 3'd3;
        if (n == 3)   return 3'd2;
        if (n == 2)   return 3'd1;
        return 3'd0;
    endfunction

    /* Classe da instrução em execute, 4 bits (fontes 0, 1 e 6).
     * Comprime as dezenas de bits is_* do exec_ctrl_t no que muda o timing. */
    typedef enum logic [3:0] {
        XC_BUBBLE = 4'd0,   // is_nop: bolha (hazard/killed dizem qual) ou FENCE
        XC_ALU    = 4'd1,
        XC_LOAD   = 4'd2,
        XC_STORE  = 4'd3,
        XC_BRANCH = 4'd4,
        XC_JAL    = 4'd5,
        XC_JALR   = 4'd6,
        XC_MUL    = 4'd7,
        XC_DIV    = 4'd8,
        XC_CSR    = 4'd9,
        XC_SYS    = 4'd10,  // ecall, ebreak, mret, wfi, sret
        XC_AMO    = 4'd11,  // só com A
        XC_OTHER  = 4'd12   // aes, zbkb, zicond, vetor...
    } exec_class_e;

    function automatic exec_class_e exec_class(input RS5_pkg::exec_ctrl_t c);
        if (c.is_nop)                                            return XC_BUBBLE;
        if (c.is_load)                                           return XC_LOAD;
        if (c.is_store)                                          return XC_STORE;
        if (c.is_beq || c.is_bne || c.is_blt || c.is_bltu
            || c.is_bge || c.is_bgeu)                            return XC_BRANCH;
        if (c.is_jalr)                                           return XC_JALR;
        if (c.is_jal_jalr)                                       return XC_JAL;
        if (c.is_mul)                                            return XC_MUL;
        if (c.is_div || c.is_rem)                                return XC_DIV;
        if (c.is_csr)                                            return XC_CSR;
        if (c.is_ecall || c.is_ebreak || c.is_mret
            || c.is_wfi || c.is_sret)                            return XC_SYS;
        if (c.is_amo || c.is_lr || c.is_sc)                      return XC_AMO;
        if (c.is_add || c.is_sub || c.is_xor || c.is_or || c.is_and
            || c.is_sll || c.is_srl || c.is_sra || c.is_slt || c.is_sltu
            || c.is_lui || c.is_auipc)                           return XC_ALU;
        return XC_OTHER;
    endfunction

    /* Classe da instrução em DECODE, a partir do opcode (bits 6:2) e de funct7[0]/funct3[2].
     * Usada nos bins de hazard (a instrução que sofre o hazard está no decode). */
    typedef enum logic [3:0] {
        DC_OTHER  = 4'd0,
        DC_LOAD   = 4'd1,
        DC_STORE  = 4'd2,
        DC_BRANCH = 4'd3,
        DC_JALR   = 4'd4,
        DC_JAL    = 4'd5,
        DC_OP     = 4'd6,   // R-type ALU
        DC_OPIMM  = 4'd7,
        DC_SYSTEM = 4'd8,   // CSR, ecall, mret...
        DC_MUL    = 4'd9,
        DC_DIV    = 4'd10,
        DC_FENCE  = 4'd11,
        DC_LUIPC  = 4'd12
    } dec_class_e;

    function automatic dec_class_e dec_class(input logic [31:0] inst);
        case (inst[6:2])
            5'b00000: return DC_LOAD;
            5'b01000: return DC_STORE;
            5'b11000: return DC_BRANCH;
            5'b11001: return DC_JALR;
            5'b11011: return DC_JAL;
            5'b01100: return inst[25] ? (inst[14] ? DC_DIV : DC_MUL) : DC_OP;
            5'b00100: return DC_OPIMM;
            5'b11100: return DC_SYSTEM;
            5'b00011: return DC_FENCE;
            5'b01101, 5'b00101: return DC_LUIPC;
            default:  return DC_OTHER;
        endcase
    endfunction

    /* Grupo de CSR (fonte 1): 16 grupos em vez de 4096 endereços. */
    function automatic logic [3:0] csr_group(input logic [11:0] a);
        case (a)
            12'h300: return 4'd0;    // mstatus
            12'h301: return 4'd1;    // misa
            12'h304: return 4'd2;    // mie
            12'h305: return 4'd3;    // mtvec
            12'h306: return 4'd4;    // mcounteren
            12'h320: return 4'd5;    // mcountinhibit
            12'h340: return 4'd6;    // mscratch
            12'h341: return 4'd7;    // mepc
            12'h342: return 4'd8;    // mcause
            12'h343: return 4'd9;    // mtval
            12'h344: return 4'd10;   // mip
            12'hB00, 12'hB02, 12'hB80, 12'hB82: return 4'd11;   // mcycle, minstret (+h)
            default: begin
                if (a[11:5] == 7'b1011000 || a[11:5] == 7'b1011100) return 4'd12; // mhpmcounter3..31 (+h)
                if (a[11:7] == 5'b11000)                             return 4'd13; // cycle/time/instret/hpmcounter (RO, U)
                if (a[11:5] == 7'b0011001)                           return 4'd14; // mhpmevent3..31
                return 4'd15;                                                      // outro / inexistente
            end
        endcase
    endfunction

endpackage

//////////////////////////////////////////////////////////////////////////////
// Uma região do mapa: contadores de 8 bits indexados por hash(key) ou por key
//////////////////////////////////////////////////////////////////////////////

module fuzz_cov_part #(
    parameter int SRC      = 0,
    parameter int BITS     = 16,     // 2**BITS posições
    parameter bit HASHED   = 1'b1,   // 1: idx = mix64(key)[BITS-1:0]; 0: idx = key[BITS-1:0]
    parameter int RAW_BITS = 96
)(
    input logic                clk,
    input logic                en,
    input logic [63:0]         key,
    input logic [RAW_BITS-1:0] raw
);
    localparam int N = 2 ** BITS;

    logic [7:0]          counts    [N];
    logic [RAW_BITS-1:0] first_raw [N];
    logic                collided  [N];   // a posição recebeu uma chave diferente da primeira

    logic [63:0]     h;
    logic [BITS-1:0] idx;
    assign h   = fuzz_cov_pkg::mix64(key);
    assign idx = HASHED ? h[BITS-1:0] : key[BITS-1:0];

    bit    enabled;
    string prefix;
    int    mask;
    int    fd;

    initial begin
        enabled = 1'b0;
        if ($value$plusargs("FUZZ_COV=%s", prefix)) begin
            if (!$value$plusargs("FUZZ_SRC=%d", mask))
                mask = -1;
            enabled = mask[SRC];
        end
        for (int i = 0; i < N; i++) begin
            counts[i]    = '0;
            first_raw[i] = '0;
            collided[i]  = 1'b0;
        end
    end

    always_ff @(posedge clk) begin
        if (enabled && en) begin
            if (counts[idx] == 8'd0)
                first_raw[idx] <= raw;
            else if (first_raw[idx] != raw)
                collided[idx] <= 1'b1;
            if (counts[idx] != 8'hFF)
                counts[idx] <= counts[idx] + 8'd1;
        end
    end

    final begin
        if (enabled) begin
            fd = $fopen($sformatf("%s_%0d.txt", prefix, SRC), "w");
            for (int i = 0; i < N; i++)
                if (counts[i] != 8'd0)
                    $fwrite(fd, "%0d %0d %0h %0d\n", i, counts[i], first_raw[i], collided[i]);
            $fclose(fd);
        end
    end
endmodule

//////////////////////////////////////////////////////////////////////////////
// Região de escalares: guarda o máximo de cada valor ao longo do caso
//////////////////////////////////////////////////////////////////////////////

module fuzz_cov_scalar #(
    parameter int SRC = 7,
    parameter int N   = 16
)(
    input logic        clk,
    input logic        en,
    input logic [31:0] values [N]
);
    logic [31:0] best [N];
    bit    enabled;
    string prefix;
    int    mask;
    int    fd;

    initial begin
        enabled = 1'b0;
        if ($value$plusargs("FUZZ_COV=%s", prefix)) begin
            if (!$value$plusargs("FUZZ_SRC=%d", mask))
                mask = -1;
            enabled = mask[SRC];
        end
        for (int i = 0; i < N; i++)
            best[i] = '0;
    end

    always_ff @(posedge clk) begin
        if (enabled && en)
            for (int i = 0; i < N; i++)
                if (values[i] > best[i])
                    best[i] <= values[i];
    end

    final begin
        if (enabled) begin
            fd = $fopen($sformatf("%s_%0d.txt", prefix, SRC), "w");
            for (int i = 0; i < N; i++)
                $fwrite(fd, "%0d %0d\n", i, best[i]);
            $fclose(fd);
        end
    end
endmodule

//////////////////////////////////////////////////////////////////////////////
// Sonda do pipeline: fontes 0 (arestas), 6 (bins) e 7 (escalares)
// Instanciada com `bind RS5 fuzz_pipe_probe ...`, enxerga os fios do RS5 pelo nome.
//////////////////////////////////////////////////////////////////////////////

module fuzz_pipe_probe
    import RS5_pkg::*;
    import fuzz_cov_pkg::*;
#(
    parameter int EDGE_BITS = 16,
    parameter int BIN_BITS  = 6,
    parameter int NSCALAR   = 8
)();

    // ---- Sinais do RS5 ----------------------------------------------------
    wire clk     = RS5.clk;
    wire reset_n = RS5.reset_n;
    wire stall   = RS5.stall;
    wire hold    = RS5.hold;

    /* fila de instruções do fetch */
    wire fq_nonempty = RS5.fetch1.buffer_tx;
    wire fq_afull    = RS5.fetch1.almost_full;
    wire fq_full     = !RS5.fetch1.buffer_not_full;
    wire valid_fetch = RS5.fetch1.valid_fetch;

    /* fetch -> decode */
    wire dec_valid   = RS5.decode_ctrl.valid;
    wire dec_jumping = RS5.decode_ctrl.jumping;
    wire bp_take     = RS5.bp_take_fetch;

    /* dentro do decode */
    wire hz_rs1   = RS5.decoder1.hazard_rs1;
    wire hz_rs2   = RS5.decoder1.hazard_rs2;
    wire hz_mem   = RS5.decoder1.hazard_mem;
    wire use_rs1  = RS5.decoder1.use_rs1;
    wire use_rs2  = RS5.decoder1.use_rs2;
    wire lock_rs1 = RS5.decoder1.locked_rs1;
    wire lock_rs2 = RS5.decoder1.locked_rs2;
    wire dec_killed = RS5.decoder1.killed;
    wire dec_hazard = RS5.decoder1.hazard;
    wire [31:0] dec_inst = RS5.instruction_decode;

    /* operandos e destinos para recalcular o forwarding (mesma prioridade do decode.sv) */
    wire [4:0] rs1       = RS5.rs1;
    wire [4:0] rs2       = RS5.rs2;
    wire [4:0] rd_exec   = RS5.rd_execute;
    wire       we_exec   = RS5.write_enable_exec;
    wire [4:0] rd_mem    = RS5.rd_mem_access;
    wire       we_mem    = RS5.ctrl_mem_access.rd_we;
    wire [4:0] rd_ret    = RS5.rd_retire;
    wire       we_ret    = RS5.ctrl_retire.rd_we;

    /* execute */
    wire exec_ctrl_t xc      = RS5.ctrl_execute;
    wire exec_valid = RS5.exec_valid;
    wire jump       = RS5.jump;
    wire rollback   = RS5.jump_rollback;
    wire exc        = RS5.RAISE_EXCEPTION;
    wire mret       = RS5.MACHINE_RETURN;
    wire irq_ack    = RS5.interrupt_ack_o;
    wire should_jump = RS5.execute1.should_jump;
    wire ill_csr    = RS5.execute1.exc_ilegal_csr_inst;
    wire ill_mret   = RS5.execute1.illegal_mret;
    wire mis_ld     = RS5.execute1.laddr_misaligned;
    wire mis_st     = RS5.execute1.saddr_misaligned;
    wire exceptionCode_e exc_code = RS5.Exception_Code;
    wire csr_we     = RS5.csr_write_enable;
    wire csr_re     = RS5.csr_read_enable;
    wire [11:0] csr_addr = RS5.csr_addr;

    /* unidades multiciclo */
    wire hold_mul = RS5.execute1.hold_mul;
    wire hold_div = RS5.execute1.hold_div;
    wire [4:0] mul_state_oh = RS5.execute1.gen_zmmul_on.mul1.mul_state;   // one-hot, 5 estados
    wire div_states_e div_state = RS5.execute1.gen_div_on.div1.state;      // 2 bits
    wire div_start  = RS5.execute1.gen_div_on.div1.start;
    wire div_by0    = RS5.execute1.gen_div_on.div1.divide_by_zero;
    wire div_ovf    = RS5.execute1.gen_div_on.div1.overflow;

    /* memória / retire */
    wire wb_ctrl_t mem_ctrl = RS5.ctrl_mem_access;
    wire wb_ctrl_t ret_ctrl = RS5.ctrl_retire;

    /* global */
    wire irq_pending = RS5.interrupt_pending;
    wire [1:0] priv  = RS5.privilege;
    wire mie         = RS5.CSRBank1.mstatus_mie;

    // ---- Campos comprimidos ------------------------------------------------

    /* nível da fila: 0 vazia, 1 com algo, 2 quase cheia, 3 cheia */
    logic [1:0] fq_level;
    assign fq_level = fq_full ? 2'd3 : fq_afull ? 2'd2 : fq_nonempty ? 2'd1 : 2'd0;

    /* origem do forwarding: 0 banco, 1 execute, 2 memória, 3 retire.
     * Só conta quando o operando é usado, não é x0 e há instrução válida no decode. */
    function automatic logic [1:0] fwd_src(input logic [4:0] rs, input logic used);
        if (!used || rs == 5'd0 || !dec_valid) return 2'd0;
        if (rs == rd_exec && we_exec)          return 2'd1;
        if (rs == rd_mem  && we_mem)           return 2'd2;
        if (rs == rd_ret  && we_ret)           return 2'd3;
        return 2'd0;
    endfunction
    logic [1:0] fwd1, fwd2;
    assign fwd1 = fwd_src(rs1, use_rs1);
    assign fwd2 = fwd_src(rs2, use_rs2);

    exec_class_e xclass;
    assign xclass = exec_class(xc);

    dec_class_e dclass;
    assign dclass = dec_class(dec_inst);

    /* estado do multiplicador: one-hot -> índice (0 IDLE, 1 ALBL, 2 ALBH, 3 AHBL, 4 AHBH) */
    logic [2:0] mul_idx;
    always_comb begin
        case (mul_state_oh)
            5'b00010: mul_idx = 3'd1;
            5'b00100: mul_idx = 3'd2;
            5'b01000: mul_idx = 3'd3;
            5'b10000: mul_idx = 3'd4;
            default:  mul_idx = 3'd0;
        endcase
    end

    /* classe do que está em memória/retire: 0 nada, 1 resultado, 2 load, 3 lr/sc */
    function automatic logic [1:0] wb_class(input wb_ctrl_t c);
        if (c.is_lr || c.is_sc) return 2'd3;
        if (c.is_load)          return 2'd2;
        if (c.rd_we)            return 2'd1;
        return 2'd0;
    endfunction

    // ---- Fonte 0: snapshot de 40 bits ----------------------------------------
    // A ordem dos campos é espelhada em riscof/fuzzer/feedback.py (SNAPSHOT_FIELDS)

    localparam int SNAP_BITS = 40;
    logic [SNAP_BITS-1:0] snap, snap_prev;

    assign snap = {
        fq_level,                                   // [39:38] fila do fetch
        dec_valid, dec_jumping, bp_take,            // [37:35]
        hz_rs1, hz_rs2, hz_mem,                     // [34:32]
        fwd1, fwd2,                                 // [31:28]
        xclass,                                     // [27:24]
        xc.hazard, xc.killed, xc.bp_taken, xc.exc_ilegal_inst,  // [23:20]
        mul_idx,                                    // [19:17]
        div_state,                                  // [16:15]
        jump, rollback, exc, mret, irq_ack,         // [14:10]
        wb_class(mem_ctrl), wb_class(ret_ctrl),     // [9:6]
        stall, irq_pending, priv, mie,              // [5:0]
        1'b0                                        // [0] reservado (alinha em 40)
    };

    /* duração: ciclos que o pipeline ficou no snapshot anterior */
    logic [15:0] run;
    logic        armed;         // 1 depois do primeiro ciclo fora do reset

    always_ff @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            snap_prev <= '0;
            run       <= 16'd0;
            armed     <= 1'b0;
        end
        else begin
            armed <= 1'b1;
            if (snap != snap_prev) begin
                snap_prev <= snap;
                run       <= 16'd1;
            end
            else if (run != 16'hFFFF)
                run <= run + 16'd1;
        end
    end

    logic        edge_now;
    logic [2:0]  dur_bucket;
    logic [63:0] edge_key;
    logic [95:0] edge_raw;

    assign edge_now   = armed && (snap != snap_prev);
    assign dur_bucket = afl_bucket(run);
    assign edge_key   = (mix64(64'(snap_prev)) >> 1) ^ mix64(64'(snap)) ^ (64'(dur_bucket) << 60);
    assign edge_raw   = {snap_prev, snap, run};

    fuzz_cov_part #(.SRC(0), .BITS(EDGE_BITS), .HASHED(1'b1), .RAW_BITS(96)) edges (
        .clk(clk), .en(edge_now), .key(edge_key), .raw(edge_raw));

    // ---- Fonte 6: bins nomeados -----------------------------------------------
    // Índices espelhados em riscof/fuzzer/feedback.py (BINS). Um bin por ciclo qualificado.

    localparam int NBINS = 2 ** BIN_BITS;
    logic [NBINS-1:0] bin_hit;

    /* hold que começa no ciclo seguinte ao fim de outro hold */
    logic hold_prev, released;
    always_ff @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            hold_prev <= 1'b0;
            released  <= 1'b0;
        end
        else begin
            hold_prev <= hold;
            released  <= hold_prev && !hold;
        end
    end

    wire act = armed && !stall;   // qualificação padrão: fora do reset e sem stall de memória

    always_comb begin
        bin_hit = '0;
        // fila do fetch
        bin_hit[0]  = act && fq_full;
        bin_hit[1]  = act && fq_full && (jump || rollback);
        bin_hit[2]  = act && rollback && !valid_fetch;
        bin_hit[3]  = act && !fq_nonempty && !dec_valid && !hold;      // fetch sem dado para o decode
        // hazards no decode
        bin_hit[4]  = act && dec_valid && lock_rs1 && !use_rs1;
        bin_hit[5]  = act && dec_valid && lock_rs2 && !use_rs2;
        bin_hit[6]  = act && dec_hazard && hz_mem;
        bin_hit[7]  = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_LOAD;
        bin_hit[8]  = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_STORE;
        bin_hit[9]  = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_BRANCH;
        bin_hit[10] = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_JALR;
        bin_hit[11] = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_OP;
        bin_hit[12] = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_OPIMM;
        bin_hit[13] = act && dec_hazard && (hz_rs1 || hz_rs2) && dclass == DC_SYSTEM;
        bin_hit[14] = act && dec_hazard && (hz_rs1 || hz_rs2) && (dclass == DC_MUL || dclass == DC_DIV);
        // forwarding
        bin_hit[15] = act && hold && (fwd1 == 2'd1 || fwd2 == 2'd1);   // da execute durante hold
        bin_hit[16] = act && hold && (fwd1 >= 2'd2 || fwd2 >= 2'd2);   // de memória/retire durante hold
        bin_hit[17] = act && fwd1 == 2'd3 && fwd2 == 2'd3;             // os dois operandos do retire
        bin_hit[18] = act && fwd1 != 2'd0 && fwd2 != 2'd0 && fwd1 != fwd2;   // origens diferentes
        // multiciclo
        bin_hit[19] = act && released && hold;                          // hold logo depois de hold
        bin_hit[20] = act && div_start && div_by0;
        bin_hit[21] = act && div_start && div_ovf;
        bin_hit[22] = act && hold && dec_hazard;                        // bolha de hazard congelada por hold
        bin_hit[23] = act && hold && (xc.killed);                       // (não deve ocorrer: killed não segura hold)
        // interrupções x classe em execute (24..35 = classe 0..11)
        bin_hit[24] = act && irq_ack && xclass == XC_BUBBLE;
        bin_hit[25] = act && irq_ack && xclass == XC_ALU;
        bin_hit[26] = act && irq_ack && xclass == XC_LOAD;
        bin_hit[27] = act && irq_ack && xclass == XC_STORE;
        bin_hit[28] = act && irq_ack && xclass == XC_BRANCH;
        bin_hit[29] = act && irq_ack && xclass == XC_JAL;
        bin_hit[30] = act && irq_ack && xclass == XC_JALR;
        bin_hit[31] = act && irq_ack && xclass == XC_MUL;
        bin_hit[32] = act && irq_ack && xclass == XC_DIV;
        bin_hit[33] = act && irq_ack && xclass == XC_CSR;
        bin_hit[34] = act && irq_ack && xclass == XC_SYS;
        bin_hit[35] = act && irq_ack && (xclass == XC_AMO || xclass == XC_OTHER);
        bin_hit[36] = act && irq_pending && mie && hold && !irq_ack;    // IRQ barrada por hold
        bin_hit[37] = armed && stall && irq_pending && mie;             // IRQ pendente durante stall
        // stall de memória (DELAY_CYCLES > 0)
        bin_hit[38] = armed && stall && hold;
        bin_hit[39] = armed && stall && should_jump;
        bin_hit[40] = armed && stall && dec_hazard;
        bin_hit[41] = armed && stall && (fwd1 != 2'd0 || fwd2 != 2'd0);
        bin_hit[42] = armed && stall && irq_ack;                        // (não deve ocorrer: ack mascarado por stall)
        // traps e privilégio
        bin_hit[43] = act && exc && priv == 2'b00;
        bin_hit[44] = act && exc && priv == 2'b11;
        bin_hit[45] = act && mret;
        bin_hit[46] = act && exc && exc_code == ECALL_FROM_UMODE;
        bin_hit[47] = act && exc && exc_code == ECALL_FROM_MMODE;
        bin_hit[48] = act && exc && exc_code == ILLEGAL_INSTRUCTION && ill_csr;
        bin_hit[49] = act && exc && exc_code == ILLEGAL_INSTRUCTION && ill_mret;
        bin_hit[50] = act && exc && exc_code == ILLEGAL_INSTRUCTION && !ill_csr && !ill_mret;
        bin_hit[51] = act && exc && (mis_ld || mis_st);
        bin_hit[52] = act && exc && exc_code == BREAKPOINT;
        bin_hit[53] = act && exc && hold_prev;                          // exceção logo depois de hold
        bin_hit[54] = act && csr_we && csr_group(csr_addr) == 4'd0 && irq_pending;   // escreve mstatus com IRQ pendente
        // previsão de desvio
        bin_hit[55] = act && rollback;                                  // previsto tomado, não tomado
        bin_hit[56] = act && exec_valid && xc.bp_taken && should_jump && !jump;   // previsto e confirmado
        bin_hit[57] = act && jump && xclass == XC_BRANCH;               // desvio tomado sem previsão
        bin_hit[58] = act && jump && xclass == XC_JALR;
        bin_hit[59] = act && jump && xclass == XC_JAL;                  // (não deve ocorrer: JAL é previsto)
        // CSR por grupo
        bin_hit[60] = act && csr_re && (csr_group(csr_addr) inside {4'd11, 4'd12, 4'd13});   // lê contadores
        bin_hit[61] = act && csr_we && csr_group(csr_addr) == 4'd6;    // escreve mscratch
        bin_hit[62] = act && csr_we && csr_group(csr_addr) == 4'd2;    // escreve mie
        bin_hit[63] = act && csr_we && csr_group(csr_addr) == 4'd0;    // escreve mstatus
    end

    /* Um fuzz_cov_part com índice direto por bin. Como só um índice é incrementado por
     * ciclo por instância, os bins são registrados em paralelo por um módulo de contadores
     * próprio (cada bin é uma posição; vários podem acertar no mesmo ciclo). */
    fuzz_cov_bins #(.SRC(6), .BITS(BIN_BITS)) bin_cnt (
        .clk(clk), .hit(bin_hit));

    // ---- Fonte 7: escalares (máximo por caso) ---------------------------------

    logic [31:0] hold_run, hz_run, stall_run, kill_run, irq_wait, fq_full_run;
    always_ff @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            hold_run <= '0; hz_run <= '0; stall_run <= '0; kill_run <= '0; irq_wait <= '0; fq_full_run <= '0;
        end
        else begin
            hold_run    <= hold                     ? hold_run + 1    : '0;
            hz_run      <= (dec_hazard && !stall)   ? hz_run + 1      : '0;
            stall_run   <= stall                    ? stall_run + 1   : '0;
            kill_run    <= (xc.killed && !stall)    ? kill_run + 1    : '0;
            irq_wait    <= (irq_pending && mie && !irq_ack) ? irq_wait + 1 : '0;
            fq_full_run <= fq_full                  ? fq_full_run + 1 : '0;
        end
    end

    logic [31:0] scalars [NSCALAR];
    assign scalars[0] = 32'(fq_level);
    assign scalars[1] = hold_run;
    assign scalars[2] = hz_run;
    assign scalars[3] = stall_run;
    assign scalars[4] = kill_run;
    assign scalars[5] = irq_wait;
    assign scalars[6] = fq_full_run;
    assign scalars[7] = 32'(run);           // maior tempo parado num mesmo snapshot

    fuzz_cov_scalar #(.SRC(7), .N(NSCALAR)) scal (
        .clk(clk), .en(armed), .values(scalars));

endmodule

//////////////////////////////////////////////////////////////////////////////
// Região de bins: um contador por bin, vários bins por ciclo
//////////////////////////////////////////////////////////////////////////////

module fuzz_cov_bins #(
    parameter int SRC  = 6,
    parameter int BITS = 6
)(
    input logic              clk,
    input logic [2**BITS-1:0] hit
);
    localparam int N = 2 ** BITS;
    logic [7:0] counts [N];
    bit    enabled;
    string prefix;
    int    mask;
    int    fd;

    initial begin
        enabled = 1'b0;
        if ($value$plusargs("FUZZ_COV=%s", prefix)) begin
            if (!$value$plusargs("FUZZ_SRC=%d", mask))
                mask = -1;
            enabled = mask[SRC];
        end
        for (int i = 0; i < N; i++)
            counts[i] = '0;
    end

    always_ff @(posedge clk) begin
        if (enabled)
            for (int i = 0; i < N; i++)
                if (hit[i] && counts[i] != 8'hFF)
                    counts[i] <= counts[i] + 8'd1;
    end

    final begin
        if (enabled) begin
            fd = $fopen($sformatf("%s_%0d.txt", prefix, SRC), "w");
            for (int i = 0; i < N; i++)
                if (counts[i] != 8'd0)
                    $fwrite(fd, "%0d %0d %0h\n", i, counts[i], i);
            $fclose(fd);
        end
    end
endmodule

//////////////////////////////////////////////////////////////////////////////
// Sonda de trap/CSR: fonte 1
// Evento: exceção, interrupção aceita, mret, escrita de CSR, leitura de CSR.
// Chave: hash(estado CSR antes, estado CSR depois, evento, contexto do pipeline).
//////////////////////////////////////////////////////////////////////////////

module fuzz_csr_probe
    import RS5_pkg::*;
    import fuzz_cov_pkg::*;
#(
    parameter int BITS = 12
)();

    wire clk     = RS5.clk;
    wire reset_n = RS5.reset_n;
    wire stall   = RS5.stall;
    wire hold    = RS5.hold;

    wire exc      = RS5.RAISE_EXCEPTION;
    wire irq_ack  = RS5.interrupt_ack_o;
    wire mret     = RS5.MACHINE_RETURN;
    wire csr_we   = RS5.csr_write_enable;
    wire csr_re   = RS5.csr_read_enable;
    wire [11:0] csr_addr = RS5.csr_addr;
    wire exceptionCode_e exc_code = RS5.Exception_Code;
    wire exec_ctrl_t xc = RS5.ctrl_execute;
    wire dec_valid = RS5.decode_ctrl.valid;
    wire irq_pending = RS5.interrupt_pending;

    /* estado de controle dos CSRs, 14 bits. Espelhado em feedback.py (CSR_STATE_FIELDS) */
    logic [13:0] cstate;
    assign cstate = {
        RS5.privilege,                                           // [13:12]
        RS5.CSRBank1.mstatus_mie, RS5.CSRBank1.mstatus_mpie,     // [11:10]
        RS5.CSRBank1.mstatus_mpp,                                // [9:8]
        RS5.CSRBank1.mie_msie, RS5.CSRBank1.mie_mtie, RS5.CSRBank1.mie_meie,   // [7:5]
        RS5.CSRBank1.mip_msip, RS5.CSRBank1.mip_mtip, RS5.CSRBank1.mip_meip,   // [4:2]
        RS5.CSRBank1.mcountinhibit[0], RS5.CSRBank1.mcountinhibit[2]           // [1:0]
    };

    /* evento (prioridade: exceção > irq > mret > escrita > leitura) */
    logic [2:0] kind;
    logic [4:0] code;
    always_comb begin
        kind = 3'd0; code = 5'd0;
        if (exc)          begin kind = 3'd1; code = 5'(exc_code); end
        else if (irq_ack) begin kind = 3'd2; code = {2'b0, RS5.CSRBank1.mip_meip, RS5.CSRBank1.mip_mtip, RS5.CSRBank1.mip_msip}; end
        else if (mret)    begin kind = 3'd3; end
        else if (csr_we)  begin kind = 3'd4; code = {1'b0, csr_group(csr_addr)}; end
        else if (csr_re)  begin kind = 3'd5; code = {1'b0, csr_group(csr_addr)}; end
    end

    /* contexto do pipeline no ciclo do evento, 8 bits */
    logic [7:0] ctx;
    assign ctx = {exec_class(xc), hold, stall, irq_pending, dec_valid};

    /* registra o evento; a chave é emitida no ciclo seguinte, com o estado "depois" */
    logic        ev_r;
    logic [13:0] before_r;
    logic [2:0]  kind_r;
    logic [4:0]  code_r;
    logic [7:0]  ctx_r;
    logic        armed;

    always_ff @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ev_r <= 1'b0; before_r <= '0; kind_r <= '0; code_r <= '0; ctx_r <= '0; armed <= 1'b0;
        end
        else begin
            armed    <= 1'b1;
            ev_r     <= armed && !stall && kind != 3'd0;
            before_r <= cstate;
            kind_r   <= kind;
            code_r   <= code;
            ctx_r    <= ctx;
        end
    end

    logic [63:0] key;
    logic [95:0] raw;
    assign raw = {52'b0, before_r, cstate, kind_r, code_r, ctx_r};   // 14+14+3+5+8 = 44 bits
    assign key = 64'(raw[43:0]);

    fuzz_cov_part #(.SRC(1), .BITS(BITS), .HASHED(1'b1), .RAW_BITS(96)) trans (
        .clk(clk), .en(ev_r), .key(key), .raw(raw));

endmodule
