# -*- coding: utf-8 -*-
"""
Created on Sun Jun 14 20:46:56 2026
注意：仅适用于直径D>1 mm的雨滴
@author: duan
"""

import numpy as np

def interpolate_from_csv(Di, filename='Raindrop_Qback_Realistic.csv'):
    """
    输入：雨滴直径Di (单位mm)，需>1 mm
    使用 numpy 读取 CSV（第一行 x，第二行 y），线性插值得到 xi 对应的 y。
    """
    data = np.loadtxt(filename, delimiter=',')          # 形状为 (2, N)
    x_vals, y_vals = data[0, :], data[1, :]             # 取出两行
    y_vals = y_vals*4*np.pi #和传统的后向散射效率Q_back定义保持一致
    return np.interp(Di, x_vals, y_vals)

if __name__ == "__main__":
    #下面仅是为了验证对于Q_back的插值结果是否正确
    Di  = [1.2, 2.3]
    Qi  = [interpolate_from_csv(xi) for xi in Di]
    import matplotlib.pyplot as plt
    filename ='Raindrop_Qback_Realistic.csv'
    data = np.loadtxt(filename, delimiter=',')
    D_vals, Q_vals = data[0, :], data[1, :]
    Q_vals = Q_vals*4*np.pi #和传统的后向散射效率Q_back定义保持一致
    plt.figure(figsize=(8, 5))    # 设置图形大小
    plt.plot(D_vals, Q_vals, label='Original', color='blue', linewidth=2)
    plt.scatter(Di, Qi, label='Interpolated', color='red')
    plt.xlabel('D (mm)')
    plt.ylabel('Q_back')
    plt.legend()
    plt.show()