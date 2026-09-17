clear;
clc;
close all;
%% 参数配置%%
v = 0;                   %设定风速m/s（反向50m/s,正向100m/s）
wavelength = 1550e-9;    %波长，固定参数
bin_width= 0.5e-9;       % bin宽度 500 ps/1ns/2ns 
T_total= 5e-6;          % 总时长（最低500ns-5us）
N_local_pers =1e5;      %本振光子秒计数率 （最低7e4,最高3e6）
N_signal_pers = 1e5;    %信号光子秒计数率 （最低7e4,最高3e6）
tau_dead = 30e-9;       %死时间，固定参数

%%单光子探测器响应%%
f_beat = 80e6 +2*v/wavelength;
n_per_cycle = 1e4*T_total/tau_dead;
P1 = N_local_pers/n_per_cycle;
P2 = N_signal_pers/n_per_cycle;
N_bin = round(T_total / bin_width);
N_d   = round(tau_dead / bin_width); % 死时间bin数目 s
t_vec = (0:N_bin-1)*bin_width;
Fs = 1/bin_width; %  a
% Step1：
R_inst = P1 + P2 + 2*sqrt(P1*P2)*cos(2*pi*f_beat * t_vec);
p = 1 - exp(-R_inst); % p(k):  S
% Step2：
q = zeros(1,N_bin);
for k = 1:N_bin
    win_start = max(1, k - N_d);
    win_idx   = win_start : k-1;
    prob_no_trigger = prod(1 - q(win_idx));
    q(k) = p(k) * prob_no_trigger;
end
% Step3: 
[corr, lags] = xcorr(q,'biased');
tau_axis = lags * bin_width;
noise_amp = 0.002;   % 噪声幅度，按需调大/调小 F 
corr_noisy = corr + noise_amp * randn(size(corr));

%%FFT%%
L = length(corr_noisy);
Y = fft(corr_noisy);
Y_half = Y(1:L/2+1);
amp_spec = abs(Y_half);
amp_spec(2:end-1) = 2*amp_spec(2:end-1);
freq_axis = (0:L/2)*Fs/L;

%% Step6：风速计算 
f_low = 10e6;
f_high = 210e6;
idx_mask = (freq_axis >= f_low) & (freq_axis <= f_high);
freq_win = freq_axis(idx_mask);
amp_win  = amp_spec(idx_mask);
[amp_peak,peak_idx_in_win] = max(amp_win);
f_peak = freq_win(peak_idx_in_win);
f_delta = f_peak - 80e6;

v_test = f_delta * wavelength / 2;
fprintf('寻峰频率 = %.3f MHz\n',f_peak/1e6);
fprintf('频率偏移Δf = %.3f MHz\n',f_delta/1e6);
fprintf('反演风速 v = %.4f m/s\n',v_test);

%% 绘图 
figure;
plot(freq_axis/1e6, amp_spec, 'k-','LineWidth',1.1); hold on;
plot(f_peak/1e6, amp_peak, 'ro','MarkerSize',6,'DisplayName','检出峰');
xlabel('频率 (MHz)'); ylabel('|FFT| 单边幅度谱');
xlim([0,220]); % 聚焦看80MHz拍频峰_Tsinghua ZGH
grid on; legend;
title('相关函数FFT 单边频谱 + 70-90MHz寻峰');

%%时间戳
t0 = 0;
t1 = 500e-9; 
[t,evt1,evt2] = single_pulse(P1,P2,0.2*P2);
edge = t0:bin_width:t1;
t_diff = evt1'-evt2;
[count,edge_out] = histcounts(t_diff,edge);
bin_center = (edge_out(1:end-1) + edge_out(2:end)) / 2;
% 简易绘图验证
figure;
bar(bin_center, count, 1);
xlabel('时间差 / s');
ylabel('光子计数');
title('时延分布直方图');
%evt2   通道2探测事件时间戳向量
function [t,evt1,evt2]  =  single_pulse(N_local,N_signal,N_bg)
%% ========== 参数设置 ==========
% SPAD 参数
dt = 500e-12;        % TDC采样步长 50ps
tau_dead = 50e-9;   % 死时间50ns
eta0 = 0.35;        % 探测效率
P_ap = 0.3;         % 原生雪崩初始后脉冲概率
P_ap_min = 1e-6;    % 后脉冲最小有效概率，低于则剔除，优化内存
N_dark = 2.5e-9;    % 暗计数均值(每bin平均光子数)
tau_ap = 100e-9;    % 后脉冲时间常数
%激光器参数
delta_f = 80e6;     % 80MHz拍频频差
bandwidth = 5e-7;   % 光开关500ns
Frequency = 10e3;   % 脉冲重复频率10kHz
%光路参数
start_bin = 1;      % 脉冲起始bin

%% Step 1：生成拍频光场平均光子数分布
t = 0: dt : 1/Frequency - dt;
N_bin = length(t);
dead_bin = round(tau_dead / dt); % 死时间占用bin数量
N_pulse_bin = round(bandwidth / dt);

phi = 2*pi*rand();
n_mean = zeros(N_bin,1);
idx_pulse = start_bin : start_bin + N_pulse_bin;
idx_pulse(idx_pulse > N_bin) = [];
% 拍频平均光子数：本振+信号+背景+干涉项
n_mean(idx_pulse) = N_local + N_signal + N_bg + 2*sqrt(N_local*N_signal)*cos(2*pi*delta_f*t(idx_pulse)+phi);
n_mean(N_pulse_bin+1:end) = N_dark;

%% Step 2：双通道泊松光子到达统计
n_real_ch1 = poissrnd(n_mean);
n_real_ch2 = poissrnd(n_mean);

%% Step3：SPAD响应仿真（死时间+概率后脉冲，Cell缓存时间戳提速）
% ========== 通道1状态 ==========
dead_cnt1 = 0;
ap_list1 = [];          % 每行[触发bin, 距触发已过bin数]
evt_cell1 = {};         % 单元数组缓存事件，避免循环频繁拼接
evt_idx1 = 1;

% ========== 通道2状态 ==========
dead_cnt2 = 0;
ap_list2 = [];
evt_cell2 = {};
evt_idx2 = 1;

for i = 1:N_bin
    curr_ph1 = n_real_ch1(i);
    new_ap1 = [];
    ap_trigger_flag = false;

    % 遍历现存后脉冲源
    for k = 1:size(ap_list1,1)
        trig_bin = ap_list1(k,1);
        delta_bin = ap_list1(k,2) + 1;
        delta_t = delta_bin * dt;
        p_now = P_ap * exp(-delta_t / tau_ap);
        
        if p_now < P_ap_min
            continue;
        end
        if rand() < p_now
            ap_trigger_flag = true;
            break; % 同一bin最多1次后脉冲触发
        else
            new_ap1 = [new_ap1; trig_bin, delta_bin];
        end
    end
    ap_list1 = new_ap1;

    % =========【重要修复】引入探测效率eta0 =========
    % 原生光子雪崩：光子存在 + 探测效率抽样成功 + 非死时间
    native_hit1 = false;
    if (dead_cnt1 <= 0) && (curr_ph1 >= 1)
        % 每个光子独立有η0概率触发雪崩，至少一个成功即触发
        photon_hits = rand(1,curr_ph1) < eta0;
        native_hit1 = any(photon_hits);
    end

    % 事件判定
    if native_hit1
        evt_cell1{evt_idx1} = t(i);
        evt_idx1 = evt_idx1 + 1;
        dead_cnt1 = dead_bin;
        % 原生雪崩产生后脉冲源
        if rand() < P_ap
            ap_list1 = [ap_list1; i, 0];
        end
    elseif ap_trigger_flag
        % 后脉冲触发，仅非死时间记录，不产生新后脉冲
        if dead_cnt1 <= 0
            evt_cell1{evt_idx1} = t(i);
            evt_idx1 = evt_idx1 + 1;
        end
    end

    % 死时间倒计时
    if dead_cnt1 > 0
        dead_cnt1 = dead_cnt1 - 1;
    end

    % ====================== 通道2独立逻辑 ======================
    curr_ph2 = n_real_ch2(i);
    new_ap2 = [];
    ap_trigger_flag2 = false;
    for k = 1:size(ap_list2,1)
        trig_bin = ap_list2(k,1);
        delta_bin = ap_list2(k,2) + 1;
        delta_t = delta_bin * dt;
        p_now = P_ap * exp(-delta_t / tau_ap);
        if p_now < P_ap_min
            continue;
        end
        if rand() < p_now
            ap_trigger_flag2 = true;
            break;
        else
            new_ap2 = [new_ap2; trig_bin, delta_bin];
        end
    end
    ap_list2 = new_ap2;

    native_hit2 = false;
    if (dead_cnt2 <= 0) && (curr_ph2 >= 1)
        photon_hits = rand(1,curr_ph2) < eta0;
        native_hit2 = any(photon_hits);
    end

    if native_hit2
        evt_cell2{evt_idx2} = t(i);
        evt_idx2 = evt_idx2 + 1;
        dead_cnt2 = dead_bin;
        if rand() < P_ap
            ap_list2 = [ap_list2; i, 0];
        end
    elseif ap_trigger_flag2
        if dead_cnt2 <= 0
            evt_cell2{evt_idx2} = t(i);
            evt_idx2 = evt_idx2 + 1;
        end
    end
    if dead_cnt2 > 0
        dead_cnt2 = dead_cnt2 - 1;
    end
end

% cell转为一维数值向量，最终输出时间戳序列
evt1 = cell2mat(evt_cell1);
evt2 = cell2mat(evt_cell2);
end

