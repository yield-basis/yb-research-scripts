# Portfolio Value for StableSwap: $x_0 + p \cdot x_1$ given $(D, p, \text{\_amp})$

## Setup

For $N = 2$ coins, let $\alpha = 2 \cdot \text{\_amp}$ (this is `Ann` in the code, equal to $A \cdot n^n$ from the whitepaper). The StableSwap invariant is:

$$\alpha(x_0 + x_1) + D = \alpha D + \frac{D^3}{4\,x_0\,x_1}$$

The price from `get_p` (computing $p = -dx_0/dx_1 > 0$ along constant $D$):

$$p = \frac{x_0\!\left(\alpha\, x_1 + \frac{D^3}{4\,x_0\,x_1}\right)}{x_1\!\left(\alpha\, x_0 + \frac{D^3}{4\,x_0\,x_1}\right)}$$

## Derivation

### Step 1: Price equation

Cross-multiplying the price formula:

$$4\alpha\,(x_0 x_1)^2(p-1) = D^3(x_0 - p\,x_1) \quad \cdots (*)$$

### Step 2: Key identity

Define $V = x_0 + p\,x_1$ (the target) and $Q = x_0 - p\,x_1$. Then:

$$V^2 = Q^2 + 4p\,x_0 x_1$$

### Step 3: Express $Q$ and $x_0 x_1$ in terms of $\Gamma$

From the invariant, define:

$$\Gamma \;\equiv\; \frac{D^3}{4\,x_0 x_1} \;=\; \alpha(S - D) + D$$

where $S = x_0 + x_1$. So $x_0 x_1 = D^3/(4\Gamma)$. From $(*)$:

$$Q = x_0 - p\,x_1 = \frac{\text{\_amp}\,(p-1)\,D^3}{2\,\Gamma^2}$$

### Step 4: Formula for $V^2$

Substituting into the identity:

$$\boxed{V^2 = \frac{p\,D^3}{\Gamma} + \frac{\text{\_amp}^2\,(p-1)^2\,D^6}{4\,\Gamma^4}}$$

### Step 5: Second relation — $V$ in terms of $S$

From $S = x_0 + x_1$ and $V = x_0 + p\,x_1$, using $x_0 = \frac{pS - V}{p - 1}$ and $x_1 = \frac{V - S}{p - 1}$:

$$(1+p)\,V = 2pS - (p-1)\,Q$$

Substituting $Q$ and $S = \bigl[\Gamma + (2\,\text{\_amp} - 1)D\bigr]/(2\,\text{\_amp})$:

$$V = \frac{p\bigl[\Gamma + (2\,\text{\_amp}-1)D\bigr]}{\text{\_amp}\,(1+p)} - \frac{\text{\_amp}\,(p-1)^2\,D^3}{2(1+p)\,\Gamma^2}$$

## Result

The two equations from Steps 4 and 5 jointly determine $V$ and $\Gamma$. Eliminating $V$ yields a single degree-6 polynomial in $\Gamma$ — **there is no simple closed-form** for $V$ in terms of $D$, $p$, $\text{\_amp}$ alone. The value must be found numerically.

### Integral representation (Envelope Theorem)

A clean exact formula follows from the envelope theorem. Since $V(p) = x_0(p) + p\,x_1(p)$:

$$\frac{dV}{dp} = \frac{dx_0}{dp} + x_1 + p\,\frac{dx_1}{dp} = x_1$$

because $dx_0 + p\,dx_1 = 0$ on the invariant curve at price $p$. Therefore:

$$V(p) = D + \int_1^p x_1(s)\,ds$$

where $V(1) = D$ (the balanced-pool value, since $x_0 = x_1 = D/2$ when $p = 1$).

## Limiting cases

| Regime | Formula |
|---|---|
| $p = 1$ (balanced) | $V = D$ |
| $\text{\_amp} \to 0$ (constant-product) | $V = D\sqrt{p}$ |
| $\text{\_amp} \to \infty$ (constant-price) | $V = D$ (only $p=1$ reachable) |

For finite $\text{\_amp}$, $V$ interpolates between $D\sqrt{p}$ and $D$. Larger $\text{\_amp}$ keeps $V$ closer to $D$ (less impermanent loss).

## Notation summary

| Symbol | Meaning |
|---|---|
| $x_0, x_1$ | Token balances (`_xp[0]`, `_xp[1]`) |
| $D$ | Invariant (from `newton_D`) |
| $\text{\_amp}$ | Amplification parameter $= A \cdot N^{N-1}$ |
| $\alpha$ | $= 2 \cdot \text{\_amp}$ (`Ann` in code) |
| $S$ | $= x_0 + x_1$ |
| $\Gamma$ | $= D^3/(4\,x_0 x_1) = \alpha(S - D) + D$ |
| $p$ | Marginal price $= -dx_0/dx_1 > 0$ |
| $V$ | Portfolio value $= x_0 + p\,x_1$ |
