# Rainfall–Runoff Methodology Equations

## 1. Inputs (per return period T)

$$
\begin{aligned}
P_T(i,j) &: \text{Precipitation depth [mm]} \\
ET_T(i,j) &: \text{Evapotranspiration [mm]} \\
S(i,j) &: \text{Soil storage capacity [mm]} \\
C(i,j) &\in [0,1] \text{ (Runoff coefficient)} \\
A_{\text{cell}} &= 100 \times 100 = 10{,}000\ \text{m}^2
\end{aligned}
$$

---

## 2. Effective & Excess Rainfall

$$
\begin{aligned}
P_e(i,j) &= \max(P_T(i,j) - ET_T(i,j), 0) \\
P_x(i,j) &= \max(P_e(i,j) - S(i,j), 0)
\end{aligned}
$$

---

## 3. Runoff Volume (Depth and Volume)

$$
\begin{aligned}
R_{\text{mm}}(i,j) &= C(i,j) \times P_x(i,j) \\
V(i,j) &= D_{\text{mm}}(i,j) \times \frac{A_{\text{cell}}}{1000} = D_{\text{mm}}(i,j) \times 10\ [\text{m}^3] \\
V_R(i,j) &= R_{\text{mm}}(i,j) \times 10
\end{aligned}
$$

---

## 4. Surplus Water

$$
\begin{aligned}
S_{\text{sur}}(i,j) &= \max(P_x(i,j) - R_{\text{mm}}(i,j), 0) \\
V_{\text{sur}}(i,j) &= S_{\text{sur}}(i,j) \times 10
\end{aligned}
$$

---

## 5. Weighted Flow Accumulation

$$
V_{\text{cum}}(i,j) = V_R(i,j) + \sum_{(m,n)\in \mathcal{U}(i,j)} V_R(m,n)
$$

---

## 6. Average Discharge Map

$$
\bar{Q}(i,j) = \frac{V_{\text{cum}}(i,j)}{3600 D}
$$

---

## 7A. Rational Method

$$
\begin{aligned}
A_{\uparrow}(i,j) &= \frac{N_{\text{upstream}} \times A_{\text{cell}}}{10^6} \\
Q_p(i,j) &= 0.278 \times \overline{C}(i,j) \times \overline{i}_D(i,j) \times A_{\uparrow}(i,j)
\end{aligned}
$$

---

## 7B. Unit Hydrograph Method

$$
\begin{aligned}
q[t] &= \sum_{k=0}^{t} e[k] \times u[t-k] \times \Delta t \\
Q_p &= \max_t q[t]
\end{aligned}
$$

---

## 8. Zonal Statistics

$$
\begin{aligned}
\overline{X}_{\Omega} &= \frac{\sum_{(i,j)\in \Omega} X(i,j) A_{\text{cell}}}{\sum_{(i,j)\in \Omega} A_{\text{cell}}} \\
V_{\Omega} &= \sum_{(i,j)\in \Omega} X_{\text{mm}}(i,j) \times \frac{A_{\text{cell}}}{1000}
\end{aligned}
$$

---

## 9. Mass Balance Check

$$
\sum_{(i,j)\in AOI} V_R(i,j) \approx V_{\text{cum}}(i_o, j_o)
$$

---

## 10. Unit Conversions

$$
\begin{aligned}
1\ \text{mm over 1 m}^2 &= 10^{-3}\ \text{m}^3 \\
1\ \text{mm over 1 ha} &= 10\ \text{m}^3
\end{aligned}
$$