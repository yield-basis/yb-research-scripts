// llamma_scan.cpp: LlamaLend liquidation-loss scan over A, on phil-svg/curve's byte-exact LLAMMA v2 engine
// (cpp-implementations/llamalend/llv2/src/llama_amm.cpp), with the method of curvefi/llamma-simulator(_v2):
//
//   For every window start (each input row, or every --stride rows) a fresh AMM is deployed with BASE_PRICE =
//   p0 * (A / (A - 1) + 1e-4), p0 the oracle at the start, and 1 collateral is deposited in bands 1..N: the oracle
//   sits just inside the top band, so the position is in soft liquidation from the start. For the window (a random
//   30..60 minutes, fixed seed) the AMM reads the external oracle each row, and one arbitrageur per row trades it to
//   the market price net of the external fee and the fee of the band it ends in (llamma-simulator_v2's
//   find_target_price), using AMM.get_amount_for_price. loss = 1 - x_down(end) / x_down(start), as
//   LendingAMM.get_all_x does; the discount adds the bands' initial value below the oracle,
//   discount = 1 - x_down(end) / (1 collateral at p0) = 1 - (1 - loss) * x_down(start) / p0.
//   Per A: the worst window and the mean of the worst --top-frac of windows (0.01%: curvefi/llamma-simulator's
//   examples average the worst 50 of 500k random windows), or of the worst --top windows.
//
// Engine use: deposit (apply_deposit), exchange (apply_token_exchange, after calc_swap_out with the same oracle),
// valuation (get_x_down), oracle limiting and fees (limit_p_o, get_dynamic_fee) are the engine's. Added here, in the
// engine's u256 style: AMM.get_amount_for_price (curve-stablecoin AMM.vy) and the constructor's MAX_ORACLE_DN_POW.
//
// Input (prepare_llamma_input.py): int64 n, then int64 t[n], double oracle[n], double market[n] (borrowed per
// collateral). Output: CSV, one row per A.
//
// usage: llamma_scan INPUT.bin OUT.csv [--A 10,20,...] [--bands 4] [--fee 0.002] [--ext-fee 0.0005]
//                    [--min-minutes 30] [--max-minutes 60] [--stride 1] [--top-frac 1e-4 | --top K] [--threads N]
//                    [--seed 1]
//                    [--trace START_TS A TRACE.csv]

#include "llama_amm.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <random>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int MAX_TICKS = 50;
constexpr int MAX_SKIP_TICKS = 1024;
const std::string USER = "position";

u256 wad(double v) { return u256(static_cast<unsigned long long>(std::llround(static_cast<long double>(v) * 1e18L))); }
double unwad(const u256& v) { return static_cast<double>(v.convert_to<long double>() / 1e18L); }
u256 sub_or_zero(const u256& a, const u256& b) { return a > b ? a - b : u256(0); }

LlammaImmutables immutables(uint64_t A, double p0) {
    LlammaImmutables im;
    im.A = A;
    im.Aminus1 = A - 1;
    im.A2 = im.A * im.A;
    im.Aminus12 = im.Aminus1 * im.Aminus1;
    im.BORROWED_PRECISION = 1;
    im.COLLATERAL_PRECISION = 1;
    const long double ratio = static_cast<long double>(A) / static_cast<long double>(A - 1);
    im.BASE_PRICE = wad(static_cast<double>(p0 * (ratio + 1e-4L)));
    im.SQRT_BAND_RATIO = u256(static_cast<unsigned long long>(std::sqrt(ratio) * 1e18L));
    im.LOG_A_RATIO = i256(static_cast<long long>(std::log(ratio) * 1e18L));
    u256 pow = ONE_1E18();  // AMM.__init__: (A / (A - 1)) ** 50
    for (int i = 0; i < 50; ++i) pow = pow * im.A / im.Aminus1;
    im.MAX_ORACLE_DN_POW = pow;
    return im;
}

// p_oracle_up(n) for the position's bands, computed once per window: with rate 0 the base price, so each band's
// oracle price, is constant (the engine's p_oracle_up evaluates exp in arbitrary precision on every call).
struct BandPrices {
    int64_t lo = 0;
    std::vector<u256> up;
    u256 operator()(const LlammaImmutables& im, const LlammaState& s, const i256& n) const {
        const int64_t k = n.convert_to<int64_t>() - lo;
        return k >= 0 && k < static_cast<int64_t>(up.size()) ? up[k] : p_oracle_up(im, s, n);
    }
};

// AMM._get_p for the active band (get_p_in_band), with the band's oracle price from the cache
u256 get_p_cached(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp, const i256& n, const u256& x,
                  const u256& y) {
    const u256 p_o_up = bp(im, s, n);
    const u256 p_o = price_oracle_ro(im, s).p;
    if (x == 0) {
        if (y == 0) return (((p_o * p_o) / p_o_up) * p_o / p_o_up) * im.A / im.Aminus1;
        return ((p_o * p_o) / p_o_up) * p_o / p_o_up;
    }
    if (y == 0) {
        const u256 p_o_down = p_o_up * im.Aminus1 / im.A;
        return (p_o * p_o) / p_o_down * p_o / p_o_down;
    }
    const u256 y0 = get_y0(im, x, y, p_o, p_o_up);
    const u256 f = im.A * y0 * p_o / p_o_up * p_o;
    const u256 g = im.Aminus1 * y0 * p_o_up / p_o;
    return (f + x * ONE_1E18()) / (g + y);
}

u256 get_p_cached(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp) {
    auto it = s.bands.find(s.active_band.convert_to<int64_t>());
    return get_p_cached(im, s, bp, s.active_band, it == s.bands.end() ? u256(0) : it->second.x,
                        it == s.bands.end() ? u256(0) : it->second.y);
}

// AMM.get_amount_for_price(p): amount to exchange for the AMM to end at price p; (amount, pump)
std::pair<u256, bool> get_amount_for_price(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp,
                                           const u256& p) {
    const u256 ONE = ONE_1E18();
    i256 n = s.active_band;
    const LimitedP p_o = price_oracle_ro(im, s);
    u256 p_o_up = bp(im, s, n);
    u256 p_down = (p_o.p * p_o.p / p_o_up) * p_o.p / p_o_up;
    u256 p_up = p_down * im.A2 / im.Aminus12;
    u256 amount = 0, y0 = 0, f = 0, g = 0, Inv = 0;
    int j = MAX_TICKS;
    bool pump = true;
    const u256 fee = std::max(s.fee, p_o.dfee);

    for (int i = 0; i < MAX_TICKS + MAX_SKIP_TICKS; ++i) {
        if (p_o_up == 0) throw std::runtime_error("get_amount_for_price: p_o_up = 0");
        auto it = s.bands.find(n.convert_to<int64_t>());
        const u256 x = it == s.bands.end() ? u256(0) : it->second.x;
        const u256 y = it == s.bands.end() ? u256(0) : it->second.y;
        if (i == 0 && p < get_p_cached(im, s, bp, n, x, y)) pump = false;
        u256 dynamic_fee = fee;
        const bool not_empty = x > 0 || y > 0;
        if (not_empty) {
            y0 = get_y0(im, x, y, p_o.p, p_o_up);
            f = (im.A * y0 * p_o.p / p_o_up) * p_o.p / ONE;
            g = im.Aminus1 * y0 * p_o_up / p_o.p;
            Inv = (f + x) * (g + y);
            if (j == MAX_TICKS) j = 0;
            dynamic_fee = std::max(get_dynamic_fee(im, p_o.p, p_o_up), fee);
        }
        const u256 antifee = (ONE * ONE) / (ONE - std::min<u256>(dynamic_fee, ONE - 1));

        if (p <= p_up && p >= p_down) {
            if (not_empty) {
                const u256 ynew = sub_or_zero(sqrt_int(Inv * ONE / p), g);
                const u256 xnew = sub_or_zero(Inv / (g + ynew), f);
                amount += pump ? sub_or_zero(xnew, x) * antifee / ONE : sub_or_zero(ynew, y) * antifee / ONE;
            }
            break;
        }
        const u256 p_ratio = p_o_up * ONE / p_o.p;
        if (pump) {
            if (not_empty) amount += ((Inv / g - f) - x) * antifee / ONE;
            if (n == s.max_band || j == MAX_TICKS - 1 || p_ratio < ONE_1E36() / im.MAX_ORACLE_DN_POW) break;
            n += 1;
            p_down = p_up;
            p_up = p_up * im.A2 / im.Aminus12;
            p_o_up = p_o_up * im.Aminus1 / im.A;
        } else {
            if (not_empty) amount += ((Inv / f - g) - y) * antifee / ONE;
            if (n == s.min_band || j == MAX_TICKS - 1 || p_ratio > im.MAX_ORACLE_DN_POW) break;
            n -= 1;
            p_up = p_down;
            p_down = p_down * im.Aminus12 / im.A2;
            p_o_up = p_o_up * im.A / im.Aminus1;
        }
        if (j != MAX_TICKS) j += 1;
    }
    return {amount, pump};  // precisions are 1: no rounding step
}

struct Arb {
    double ext_fee;
};

// The fee a trade pays in band n: max(fee, the oracle-move fee, the band's distance fee), as calc_swap_out charges
double band_fee(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp, const LimitedP& p_o,
                const i256& n) {
    return unwad(std::max({s.fee, p_o.dfee, get_dynamic_fee(im, p_o.p, bp(im, s, n))}));
}

double p_current_down(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp, const LimitedP& p_o,
                      const i256& n) {
    const u256 up = bp(im, s, n);
    return unwad((p_o.p * p_o.p / up) * p_o.p / up);
}

// The AMM price an arbitrageur trades to at external price p (already net of the external fee): llamma-simulator_v2's
// find_target_price. Going up, the highest band whose current down price is below p net of its fee; going down, the
// lowest band whose current up price is above p plus its fee.
double target_price(const LlammaImmutables& im, const LlammaState& s, const BandPrices& bp, const LimitedP& p_o,
                    double p, bool up) {
    const double band_ratio = unwad(im.A2 * ONE_1E18() / im.Aminus12);  // p_current_up / p_current_down
    if (up) {
        for (i256 n = s.max_band; n >= s.min_band; n -= 1) {
            const double target = p * (1 - band_fee(im, s, bp, p_o, n));
            if (target > p_current_down(im, s, bp, p_o, n)) return target;
        }
        return p * (1 - band_fee(im, s, bp, p_o, s.min_band));
    }
    for (i256 n = s.min_band; n <= s.max_band; n += 1) {
        const double target = p / (1 - band_fee(im, s, bp, p_o, n));
        if (target < p_current_down(im, s, bp, p_o, n) * band_ratio) return target;
    }
    return p / (1 - band_fee(im, s, bp, p_o, s.max_band));
}

// Exchange the amount AMM.get_amount_for_price gives for `target`, if it moves the AMM the intended way.
bool trade_to(const LlammaImmutables& im, LlammaState& s, const BandPrices& bp, const LimitedP& p_o, double target,
              bool pump) {
    auto [amount, is_pump] = get_amount_for_price(im, s, bp, wad(target));
    if (amount == 0 || is_pump != pump) return false;
    DetailedTrade tr = calc_swap_out(im, s, pump, amount, p_o.p, p_o.dfee, 1, 1);
    if (tr.in_amount == 0 || tr.out_amount == 0) return false;
    apply_token_exchange(im, s, pump ? 0 : 1, pump ? 1 : 0, tr.in_amount, tr.out_amount);
    return true;
}

// One arbitrageur at market price p_mkt, as llamma-simulator_v2's calculate_loss does for a candle whose high and low
// are both p_mkt: up to the market net of fees if the AMM is below it, then down if it is above. Every band's fee is
// at least max(fee, oracle-move fee), so when the market net of that cannot move the AMM no band target can: the band
// search is skipped (the same trades, without the search on quiet rows).
int arbitrage(const LlammaImmutables& im, LlammaState& s, const BandPrices& bp, double p_mkt, const Arb& arb) {
    int trades = 0;
    const LimitedP p_o = price_oracle_ro(im, s);
    const double fee_min = unwad(std::max(s.fee, p_o.dfee));
    const double p_up = p_mkt * (1 - arb.ext_fee), p_down = p_mkt * (1 + arb.ext_fee);
    double p_amm = unwad(get_p_cached(im, s, bp));
    if (p_up * (1 - fee_min) > p_amm) {
        const double target = target_price(im, s, bp, p_o, p_up, true);
        if (target > p_amm && trade_to(im, s, bp, p_o, target, true)) {
            trades += 1;
            p_amm = unwad(get_p_cached(im, s, bp));
        }
    }
    if (p_down / (1 - fee_min) < p_amm) {
        const double target = target_price(im, s, bp, p_o, p_down, false);
        if (target < p_amm) trades += trade_to(im, s, bp, p_o, target, false);
    }
    return trades;
}

struct Input {
    std::vector<int64_t> t;
    std::vector<double> oracle, market;
};

Input read_input(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("cannot read " + path);
    int64_t n = 0;
    f.read(reinterpret_cast<char*>(&n), sizeof n);
    Input in;
    in.t.resize(n);
    in.oracle.resize(n);
    in.market.resize(n);
    f.read(reinterpret_cast<char*>(in.t.data()), n * sizeof(int64_t));
    f.read(reinterpret_cast<char*>(in.oracle.data()), n * sizeof(double));
    f.read(reinterpret_cast<char*>(in.market.data()), n * sizeof(double));
    if (!f) throw std::runtime_error("short input " + path);
    return in;
}

struct WindowResult {
    double loss = std::nan(""), discount = std::nan(""), recovery0 = std::nan("");
    int trades = 0;
};

// Simulate one window [i0, i1] at A with N bands. trace: optional per-row log.
WindowResult run_window(const Input& in, size_t i0, size_t i1, uint64_t A, int bands, double fee, const Arb& arb,
                        std::FILE* trace = nullptr) {
    const double p0 = in.oracle[i0];
    const LlammaImmutables im = immutables(A, p0);
    LlammaState s;
    s.fee = wad(fee);
    s.rate = 0;
    s.rate_time = u256(static_cast<uint64_t>(in.t[i0]));
    s.block_timestamp = u256(static_cast<uint64_t>(in.t[i0]));
    s.external_price = wad(p0);
    s.old_p_o = wad(p0);  // zero fee memory at the start (llamma-simulator_v2's causal initialization)
    s.old_dfee = 0;
    s.prev_p_o_time = s.block_timestamp;
    apply_deposit(im, s, USER, ONE_1E18(), i256(1), i256(bands));
    BandPrices bp;
    bp.lo = 0;
    for (int n = 0; n <= bands + 1; ++n) bp.up.push_back(p_oracle_up(im, s, i256(n)));
    WindowResult r;
    const double x0 = unwad(get_x_down(im, s, USER));
    r.recovery0 = x0 / p0;
    for (size_t i = i0; i <= i1; ++i) {
        s.block_timestamp = u256(static_cast<uint64_t>(in.t[i]));
        s.external_price = wad(in.oracle[i]);
        r.trades += arbitrage(im, s, bp, in.market[i], arb);
        if (trace) {
            auto [x, y] = get_sum_xy(im, s, USER);
            std::fprintf(trace, "%lld,%.12g,%.12g,%.12g,%.12g,%.12g,%.12g,%lld\n", static_cast<long long>(in.t[i]),
                         in.oracle[i], in.market[i], unwad(get_p(im, s)), unwad(x), unwad(y),
                         unwad(get_x_down(im, s, USER)), static_cast<long long>(s.active_band.convert_to<int64_t>()));
        }
    }
    const double x1 = unwad(get_x_down(im, s, USER));
    r.loss = 1 - x1 / x0;
    r.discount = 1 - x1 / p0;
    return r;
}

std::vector<uint64_t> parse_list(const std::string& s) {
    std::vector<uint64_t> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, ',')) out.push_back(std::stoull(item));
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: %s INPUT.bin OUT.csv [options]\n", argv[0]);
        return 1;
    }
    std::vector<uint64_t> As = {10, 15, 20, 30, 40, 50, 70, 100, 140, 200, 300, 400, 500, 700, 1000};
    int bands = 4, top = 0, stride = 1;
    double top_frac = 1e-4;
    double fee = 0.002, ext_fee = 5e-4, min_minutes = 30, max_minutes = 60;
    unsigned threads = std::max(1u, std::thread::hardware_concurrency());
    uint64_t seed = 1;
    long long trace_ts = -1;
    uint64_t trace_A = 0;
    std::string trace_path;
    for (int i = 3; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) throw std::runtime_error("missing value for " + a);
            return argv[++i];
        };
        if (a == "--A") As = parse_list(next());
        else if (a == "--bands") bands = std::stoi(next());
        else if (a == "--fee") fee = std::stod(next());
        else if (a == "--ext-fee") ext_fee = std::stod(next());
        else if (a == "--min-minutes") min_minutes = std::stod(next());
        else if (a == "--max-minutes") max_minutes = std::stod(next());
        else if (a == "--stride") stride = std::stoi(next());
        else if (a == "--top") top = std::stoi(next());
        else if (a == "--top-frac") top_frac = std::stod(next());
        else if (a == "--threads") threads = std::stoul(next());
        else if (a == "--seed") seed = std::stoull(next());
        else if (a == "--trace") { trace_ts = std::stoll(next()); trace_A = std::stoull(next()); trace_path = next(); }
        else throw std::runtime_error("unknown option " + a);
    }
    const Input in = read_input(argv[1]);
    const size_t n = in.t.size();
    const Arb arb{ext_fee};

    // Window ends: a fixed-seed duration in [min, max] minutes per start
    std::vector<size_t> starts, ends;
    std::mt19937_64 rng(seed);
    std::uniform_real_distribution<double> dur(min_minutes * 60, max_minutes * 60);
    for (size_t i = 0; i < n; i += stride) {
        const int64_t t_end = in.t[i] + static_cast<int64_t>(dur(rng));
        size_t j = std::lower_bound(in.t.begin() + i, in.t.end(), t_end) - in.t.begin();
        if (j >= n) break;
        starts.push_back(i);
        ends.push_back(j);
    }

    if (trace_ts >= 0) {
        size_t i0 = std::lower_bound(in.t.begin(), in.t.end(), trace_ts) - in.t.begin();
        size_t k = std::lower_bound(starts.begin(), starts.end(), i0) - starts.begin();
        std::FILE* f = std::fopen(trace_path.c_str(), "w");
        std::fprintf(f, "timestamp,oracle,market,amm_price,x,y,x_down,active_band\n");
        WindowResult r = run_window(in, starts[k], ends[k], trace_A, bands, fee, arb, f);
        std::fclose(f);
        std::fprintf(stderr, "trace A=%llu from %lld: loss %.6f discount %.6f trades %d -> %s\n",
                     static_cast<unsigned long long>(trace_A), static_cast<long long>(in.t[starts[k]]), r.loss,
                     r.discount, r.trades, trace_path.c_str());
        return 0;
    }

    std::FILE* out = std::fopen(argv[2], "w");
    std::fprintf(out, "A,bands,fee,ext_fee,windows,failed,loss_top1,loss_topN,discount_top1,discount_topN,"
                      "recovery0_mean,worst_start_ts,worst_end_ts,trades_mean,top\n");
    for (uint64_t A : As) {
        std::vector<WindowResult> res(starts.size());
        std::atomic<size_t> next{0}, failed{0};
        std::vector<std::thread> pool;
        for (unsigned w = 0; w < threads; ++w) {
            pool.emplace_back([&]() {
                for (size_t k; (k = next.fetch_add(1)) < starts.size();) {
                    try {
                        res[k] = run_window(in, starts[k], ends[k], A, bands, fee, arb);
                    } catch (const std::exception&) {
                        failed += 1;
                    }
                }
            });
        }
        for (auto& th : pool) th.join();

        std::vector<size_t> order;
        double rec = 0, trades = 0;
        for (size_t k = 0; k < res.size(); ++k) {
            if (std::isnan(res[k].loss)) continue;
            order.push_back(k);
            rec += res[k].recovery0;
            trades += res[k].trades;
        }
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return res[a].loss > res[b].loss; });
        const size_t want = top > 0 ? static_cast<size_t>(top)
                                    : std::max<size_t>(1, std::llround(top_frac * order.size()));
        const size_t m = std::min<size_t>(want, order.size());
        double loss_n = 0, disc_n = 0;
        for (size_t q = 0; q < m; ++q) {
            loss_n += res[order[q]].loss;
            disc_n += res[order[q]].discount;
        }
        const size_t w = order[0];
        std::fprintf(out, "%llu,%d,%g,%g,%zu,%zu,%.8f,%.8f,%.8f,%.8f,%.8f,%lld,%lld,%.3f,%zu\n",
                     static_cast<unsigned long long>(A), bands, fee, ext_fee, order.size(), failed.load(),
                     res[w].loss, loss_n / m, res[w].discount, disc_n / m, rec / order.size(),
                     static_cast<long long>(in.t[starts[w]]), static_cast<long long>(in.t[ends[w]]),
                     trades / order.size(), m);
        std::fflush(out);
        std::fprintf(stderr, "A=%llu: windows %zu (failed %zu), loss top1 %.4f topN %.4f, discount top1 %.4f topN %.4f\n",
                     static_cast<unsigned long long>(A), order.size(), failed.load(), res[w].loss, loss_n / m,
                     res[w].discount, disc_n / m);
    }
    std::fclose(out);
    return 0;
}
