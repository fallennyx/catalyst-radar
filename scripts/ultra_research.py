"""Ultra research backtest analysis for alert_backtest.csv.

Quant research harness: computes conditional win rates, lifts, Mann-Whitney U
tests, interaction stacks, regimes, robustness checks. Output is plain text
to stdout, consumed by the human analyst for the final report.
"""
from __future__ import annotations

import warnings
import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

warnings.filterwarnings("ignore")
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 60)

CSV = "data/alert_backtest.csv"
BASELINE_WR = 0.407  # +4h win rate baseline from brief

R1 = "signed_ret_+1h_pct"
R2 = "signed_ret_+2h_pct"
R4 = "signed_ret_+4h_pct"
R8 = "signed_ret_+8h_pct"
HORIZONS = [R1, R2, R4, R8]


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["alert_utc"] = pd.to_datetime(df["alert_utc"])
    df["date"] = df["alert_utc"].dt.date
    # drop the 1 fully-empty forward-return row (no horizons computable)
    df = df[~df[HORIZONS].isna().all(axis=1)].reset_index(drop=True)
    # derived
    df["max_ret"] = df[HORIZONS].max(axis=1)
    df["min_ret"] = df[HORIZONS].min(axis=1)
    # time to peak: which horizon gave best signed return
    hz_map = {R1: 1, R2: 2, R4: 4, R8: 8}
    df["peak_hz"] = df[HORIZONS].idxmax(axis=1, skipna=True).map(hz_map)
    # hit +1% before +4h close (proxy: best of 1h/2h/4h >= 1)
    df["hit_1pct"] = (df[[R1, R2, R4]].max(axis=1) >= 1.0).astype(int)
    # binary win flags
    df["win_1h"] = (df[R1] > 0).astype(int)
    df["win_4h"] = (df[R4] > 0).astype(int)
    df["win_8h"] = (df[R8] > 0).astype(int)
    # path types
    df["reversal"] = ((df[R1] > 0) & (df[R4] < 0)).astype(int)
    df["persistence"] = ((df[R4] > 0) & (df[R8] > 0)).astype(int)
    df["frontloaded"] = (df[R1] > df[R4]).astype(int)
    # regimes
    df["aligned"] = (
        ((df["direction"] == "long") & (df["btc_ret_4h_pct"] > 0))
        | ((df["direction"] == "short") & (df["btc_ret_4h_pct"] < 0))
    ).astype(int)
    return df


def desc(s: pd.Series, ret_col: str = R4) -> dict:
    """Return summary stats block for a subset's return column."""
    s = s.dropna()
    n = len(s)
    if n == 0:
        return dict(n=0, wr=np.nan, avg=np.nan, med=np.nan, std=np.nan, lift=np.nan)
    wr = (s > 0).mean()
    return dict(
        n=n,
        wr=wr,
        avg=s.mean(),
        med=s.median(),
        std=s.std(),
        lift=wr / BASELINE_WR,
    )


def fmt(d: dict) -> str:
    return (
        f"N={d['n']:>4}  WR={d['wr']*100:5.1f}%  lift={d['lift']:4.2f}  "
        f"avg={d['avg']:+6.2f}%  med={d['med']:+6.2f}%  std={d['std']:5.2f}"
    )


def mw(a: pd.Series, b: pd.Series) -> str:
    """Mann-Whitney U two-sided p-value between two return samples."""
    a, b = a.dropna(), b.dropna()
    if len(a) < 5 or len(b) < 5:
        return "p=n/a (small)"
    try:
        _, p = mannwhitneyu(a, b, alternative="two-sided")
        flag = " *" if p < 0.05 else ""
        return f"p={p:.4f}{flag}"
    except ValueError:
        return "p=n/a"


def hdr(title: str):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def bucket_report(df, col, bins, labels, ret_col=R4):
    print(f"\n-- {col} buckets vs {ret_col} --")
    cat = pd.cut(df[col], bins=bins, labels=labels)
    for lab in labels:
        sub = df[cat == lab]
        if len(sub) == 0:
            continue
        print(f"  {str(lab):>14}: {fmt(desc(sub[ret_col]))}")


def cat_report(df, col, ret_col=R4, min_n=1, order=None):
    print(f"\n-- {col} vs {ret_col} --")
    vals = order if order else sorted(df[col].dropna().unique())
    for v in vals:
        sub = df[df[col] == v]
        if len(sub) < min_n:
            continue
        tag = "  (N<20!)" if len(sub) < 20 else ""
        print(f"  {str(v):>14}: {fmt(desc(sub[ret_col]))}{tag}")


def main():
    df = load()
    pd.options.mode.chained_assignment = None

    hdr("0. DATASET OVERVIEW & BASELINES")
    print(f"Rows: {len(df)}")
    for c in HORIZONS + ["max_ret"]:
        s = df[c].dropna()
        print(f"  {c:>22}: N={len(s)} WR={(s>0).mean()*100:5.1f}% "
              f"avg={s.mean():+.3f}% med={s.median():+.3f}% std={s.std():.3f}")
    print(f"\nDirection: {df['direction'].value_counts().to_dict()}")
    print(f"Asset class: {df['asset_class'].value_counts().to_dict()}")
    print(f"structure_type: {df['structure_type'].value_counts().to_dict()}")
    print(f"db windows: {df['db'].value_counts().to_dict()}")
    print(f"\nProfit paths:")
    print(f"  early win held (1h>0 & 4h>0):     {((df[R1]>0)&(df[R4]>0)).sum()}")
    print(f"  early loss recovered (1h<0 & 4h>0):{((df[R1]<0)&(df[R4]>0)).sum()}")
    print(f"  early win reversed (1h>0 & 4h<0): {((df[R1]>0)&(df[R4]<0)).sum()}")
    print(f"  consistent loss (1h<0 & 4h<0):    {((df[R1]<0)&(df[R4]<0)).sum()}")
    print(f"  peak horizon distribution: {df['peak_hz'].value_counts().sort_index().to_dict()}")
    print(f"  hit +1% (best of 1h/2h/4h>=1%): {df['hit_1pct'].mean()*100:.1f}% ({df['hit_1pct'].sum()})")

    # ---------- A. BOS Structure Quality ----------
    hdr("A. BOS STRUCTURE QUALITY")
    cat_report(df, "structure_type", R4)
    cat_report(df, "structure_type", R1)
    print("\nMann-Whitney 4h_and_1h vs 4h (+4h):",
          mw(df[df.structure_type=="4h_and_1h"][R4], df[df.structure_type=="4h"][R4]))
    bins = [-1e9, 1, 3, 6, 1e9]
    labs = ["<1%", "1-3%", "3-6%", ">6%"]
    bucket_report(df, "breakout_dist_pct", bins, labs, R4)
    bucket_report(df, "breakout_dist_pct", bins, labs, R1)
    print("\nCorrelation breakout_dist_pct vs +4h:",
          round(df["breakout_dist_pct"].corr(df[R4], method="spearman"), 4), "(spearman)")

    # ---------- B. Alpha-Z ----------
    hdr("B. ALPHA-Z DECOUPLING")
    df["abs_az"] = df["alpha_z"].abs()
    bins = [0, 2, 3, 5, 1e9]
    labs = ["<2", "2-3", "3-5", ">5"]
    bucket_report(df, "abs_az", bins, labs, R4)
    bucket_report(df, "abs_az", bins, labs, R1)
    print("\nSpearman |alpha_z| vs +4h:", round(df["abs_az"].corr(df[R4], method="spearman"), 4))
    print("Spearman |r_alpha_pct| vs +4h:", round(df["r_alpha_pct"].abs().corr(df[R4], method="spearman"), 4))
    print("\nBy direction, |alpha_z|>=3:")
    for d in ["long", "short"]:
        sub = df[(df.direction==d)&(df.abs_az>=3)]
        print(f"  {d:>5}: {fmt(desc(sub[R4]))}")
    print("\nalpha_z>=3 X score_pctile granular (+4h):")
    for az in [(2,3),(3,5),(5,99)]:
        for sp in [(0,50),(50,75),(75,101)]:
            sub = df[(df.abs_az>=az[0])&(df.abs_az<az[1])&(df.score_pctile>=sp[0])&(df.score_pctile<sp[1])]
            if len(sub)>0:
                print(f"  az[{az[0]},{az[1]}) pctile[{sp[0]},{sp[1]}): {fmt(desc(sub[R4]))}")

    # ---------- C. Market Regime via BTC ----------
    hdr("C. MARKET REGIME VIA BTC")
    cat_report(df, "aligned", R4, order=[1,0])
    print("  (aligned=1 means alert dir agrees w/ btc_ret_4h sign)")
    bins=[-1e9,-1,0,1,2,1e9]; labs=["<-1%","-1..0","0..1%","1..2%",">2%"]
    bucket_report(df, "btc_ret_4h_pct", bins, labs, R4)
    cat_report(df, "btc_range_expansion", R4, order=[1,0])
    print("\nSame-timescale predictor test:")
    print("  Spearman btc_ret_1h vs +1h:", round(df["btc_ret_1h_pct"].corr(df[R1], method="spearman"),4))
    print("  Spearman btc_ret_4h vs +4h:", round(df["btc_ret_4h_pct"].corr(df[R4], method="spearman"),4))
    print("\nLong alerts by btc_ret_4h sign:")
    for lab,mask in [("btc up",df.btc_ret_4h_pct>0),("btc down",df.btc_ret_4h_pct<0)]:
        sub=df[(df.direction=="long")&mask]
        print(f"  long & {lab}: {fmt(desc(sub[R4]))}")

    # ---------- D. Cluster Size ----------
    hdr("D. CLUSTER SIZE AS FALSE-SIGNAL FILTER")
    cat_report(df, "cluster_size", R4)
    cat_report(df, "cluster_size", R1)
    print("\nisolated (=1) vs clustered (>=3):")
    for h in HORIZONS:
        iso=df[df.cluster_size==1][h]; clu=df[df.cluster_size>=3][h]
        print(f"  {h}: iso WR={ (iso>0).mean()*100:.1f}% (N={len(iso)}) avg={iso.mean():+.2f}  "
              f"clu WR={(clu>0).mean()*100:.1f}% (N={len(clu)}) avg={clu.mean():+.2f}  {mw(iso,clu)}")
    print("\ncluster_size X asset_class (+4h):")
    for ac in df.asset_class.unique():
        for cl in [("iso",df.cluster_size==1),("clu>=3",df.cluster_size>=3)]:
            sub=df[(df.asset_class==ac)&cl[1]]
            if len(sub)>0:
                print(f"  {ac:>12} {cl[0]:>6}: {fmt(desc(sub[R4]))}")

    # ---------- E. Score percentile vs raw ----------
    hdr("E. SCORE PERCENTILE VS RAW SCORE")
    bins=[-1,25,50,75,101]; labs=["0-25","25-50","50-75","75-100"]
    bucket_report(df, "score_pctile", bins, labs, R4)
    for thr in [5,8,10,12]:
        sub=df[df.score>=thr]
        print(f"  score>={thr}: {fmt(desc(sub[R4]))}")
    print("\nSpearman score_pctile vs +4h:", round(df["score_pctile"].corr(df[R4],method="spearman"),4))
    print("Spearman score vs +4h:", round(df["score"].corr(df[R4],method="spearman"),4))
    print("Spearman score vs |+4h| (magnitude):", round(df["score"].corr(df[R4].abs(),method="spearman"),4))
    print("Spearman score_pctile vs |+4h|:", round(df["score_pctile"].corr(df[R4].abs(),method="spearman"),4))

    # ---------- F. Already running ----------
    hdr("F. ALREADY RUNNING VS FRESH BREAKOUT")
    bins=[-1e9,0,3,8,1e9]; labs=["neg","0-3%","3-8%",">8%"]
    bucket_report(df, "ticker_ret_4h_pct", bins, labs, R4)
    bucket_report(df, "ticker_ret_4h_pct", bins, labs, R1)
    print("\nshort alerts where ticker already up >8% (counter-trend exhaustion):")
    sub=df[(df.direction=="short")&(df.ticker_ret_4h_pct>8)]
    print(f"  {fmt(desc(sub[R4]))}")
    sub=df[(df.direction=="short")&(df.ticker_ret_4h_pct< -8)]
    print(f"  short & ticker down >8%: {fmt(desc(sub[R4]))}")
    print("Spearman ticker_ret_4h vs +4h:", round(df["ticker_ret_4h_pct"].corr(df[R4],method="spearman"),4))

    # ---------- G. Time of day / DOW ----------
    hdr("G. TIME-OF-DAY & DAY-OF-WEEK")
    sessions = {
        "Asia 00-08": (df.alert_hour>=0)&(df.alert_hour<8),
        "EU 08-13": (df.alert_hour>=8)&(df.alert_hour<13),
        "USopen 13-17": (df.alert_hour>=13)&(df.alert_hour<17),
        "USpm 17-21": (df.alert_hour>=17)&(df.alert_hour<21),
        "Overnight 21-24": (df.alert_hour>=21),
    }
    for name,mask in sessions.items():
        print(f"  {name:>16}: {fmt(desc(df[mask][R4]))}")
    print("\nper-hour (N>=10):")
    cat_report(df, "alert_hour", R4, min_n=10)
    print("\nday of week:")
    dow_order=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    cat_report(df, "alert_dow", R4, order=dow_order)

    # ---------- H. Repeat fire ----------
    hdr("H. REPEAT-FIRE ANALYSIS")
    cat_report(df, "repeat_fire_8h", R4, order=[0,1])
    cat_report(df, "repeat_fire_8h", R1, order=[0,1])
    print("\nrepeat_fire breakout_dist_pct means:")
    print(f"  first fire: {df[df.repeat_fire_8h==0]['breakout_dist_pct'].mean():.2f}%")
    print(f"  repeat:     {df[df.repeat_fire_8h==1]['breakout_dist_pct'].mean():.2f}%")
    print(mw(df[df.repeat_fire_8h==0][R4], df[df.repeat_fire_8h==1][R4]))

    # ---------- I. Vol ratio ----------
    hdr("I. VOL RATIO ANALYSIS")
    bins=[0,2,5,15,1e9]; labs=["1-2x","2-5x","5-15x",">15x"]
    bucket_report(df, "vol_ratio", bins, labs, R4)
    print("\nSpearman vol_ratio vs +4h:", round(df["vol_ratio"].corr(df[R4],method="spearman"),4))
    print("Spearman vol_ratio vs |+4h|:", round(df["vol_ratio"].corr(df[R4].abs(),method="spearman"),4))
    print("\nvol_ratio X alpha_z (+4h):")
    for vr in [("vol>=7",df.vol_ratio>=7),("vol<7",df.vol_ratio<7)]:
        for az in [("az>=3",df.abs_az>=3),("az<3",df.abs_az<3)]:
            sub=df[vr[1]&az[1]]
            print(f"  {vr[0]:>7} {az[0]:>6}: {fmt(desc(sub[R4]))}")

    # ---------- J. Asset class failure modes ----------
    hdr("J. ASSET CLASS FAILURE MODES")
    for ac in df.asset_class.value_counts().index:
        sub=df[df.asset_class==ac][R4].dropna()
        if len(sub)==0: continue
        print(f"  {ac:>12}: {fmt(desc(sub))}  "
              f"P(>+5%)={ (sub>5).mean()*100:.1f}% P(<-5%)={(sub< -5).mean()*100:.1f}%")
    # t2 outlier robustness
    t2=df[df.asset_class=="crypto_t2"][R4].dropna()
    print(f"\ncrypto_t2 mean={t2.mean():+.3f}% median={t2.median():+.3f}% "
          f"trimmed5% mean={t2[(t2>t2.quantile(.05))&(t2<t2.quantile(.95))].mean():+.3f}%")

    # ---------- K. Profit path ----------
    hdr("K. PROFIT PATH ANALYSIS")
    paths = {
        "early win held":   (df[R1]>0)&(df[R4]>0),
        "early loss recov": (df[R1]<0)&(df[R4]>0),
        "early win reversed":(df[R1]>0)&(df[R4]<0),
        "consistent loss":  (df[R1]<0)&(df[R4]<0),
    }
    for name,mask in paths.items():
        sub=df[mask]
        print(f"  {name:>18}: n={len(sub):>3} ({len(sub)/len(df)*100:4.1f}%) "
              f"avg+4h={sub[R4].mean():+.2f}%")
    print("\nWhat predicts 'early win reversed' vs 'early win held' (feature means):")
    rev=df[(df[R1]>0)&(df[R4]<0)]; held=df[(df[R1]>0)&(df[R4]>0)]
    for f in ["score_pctile","abs_az","cluster_size","breakout_dist_pct","vol_ratio","ticker_ret_4h_pct","btc_ret_4h_pct"]:
        print(f"  {f:>18}: reversed={rev[f].mean():+.3f}  held={held[f].mean():+.3f}  {mw(rev[f],held[f])}")

    # ---------- L. Outlier anatomy ----------
    hdr("L. OUTLIER ANATOMY")
    top=df.nlargest(10,R4); bot=df.nsmallest(10,R4)
    cols=["ticker","asset_class","score","score_pctile","alpha_z","cluster_size",
          "breakout_dist_pct","vol_ratio","structure_type","btc_ret_4h_pct","ticker_ret_4h_pct",R1,R4,R8]
    print("TOP 10 winners (+4h):")
    print(top[cols].to_string(index=False))
    print("\nBOTTOM 10 losers (+4h):")
    print(bot[cols].to_string(index=False))
    print("\nFeature means: top10 vs bottom10 vs all:")
    for f in ["score","score_pctile","abs_az","cluster_size","breakout_dist_pct","vol_ratio","ticker_ret_4h_pct"]:
        print(f"  {f:>18}: top={top[f].mean():+.3f}  bot={bot[f].mean():+.3f}  all={df[f].mean():+.3f}")
    # trimmed robustness
    lo,hi=df[R4].quantile(.05),df[R4].quantile(.95)
    trim=df[(df[R4]>lo)&(df[R4]<hi)]
    print(f"\nTrimmed (5/95) dataset: {fmt(desc(trim[R4]))}")
    print(f"Full dataset:           {fmt(desc(df[R4]))}")

    # ---------- M. Triple stack ----------
    hdr("M. TRIPLE-STACK INTERACTION")
    stack=df[(df.abs_az>=3)&(df.score_pctile>=75)&(df.cluster_size==1)]
    print(f"alpha_z>=3 & pctile>=75 & cluster=1:")
    for h in HORIZONS:
        s=stack[h]
        print(f"  {h}: {fmt(desc(s))}")
    print(f"\nstack vs rest (+4h): {mw(stack[R4], df[~df.index.isin(stack.index)][R4])}")
    print("\nstack X asset_class (+4h):")
    for ac in stack.asset_class.unique():
        print(f"  {ac:>12}: {fmt(desc(stack[stack.asset_class==ac][R4]))}")
    print("\nstack X btc_range_expansion:")
    for v in [0,1]:
        print(f"  bre={v}: {fmt(desc(stack[stack.btc_range_expansion==v][R4]))}")
    print("\n4th-variable search on top of triple stack (+4h):")
    fourth = {
        "dist<2%": stack.breakout_dist_pct<2,
        "ticker_ret 3-8%": (stack.ticker_ret_4h_pct>=3)&(stack.ticker_ret_4h_pct<=8),
        "direction=long": stack.direction=="long",
        "vol>=7": stack.vol_ratio>=7,
        "bre=0": stack.btc_range_expansion==0,
        "EU+USopen hr 8-17": (stack.alert_hour>=8)&(stack.alert_hour<17),
    }
    for name,mask in fourth.items():
        print(f"  + {name:>18}: {fmt(desc(stack[mask][R4]))}")

    # ---------- Interaction search (2 & 3 var) ----------
    hdr("N. SYSTEMATIC INTERACTION SEARCH")
    feats = {
        "az>=3": df.abs_az>=3,
        "pctile>=75": df.score_pctile>=75,
        "cluster=1": df.cluster_size==1,
        "dist<2%": df.breakout_dist_pct<2,
        "long": df.direction=="long",
        "vol>=7": df.vol_ratio>=7,
        "t2": df.asset_class=="crypto_t2",
        "bre=0": df.btc_range_expansion==0,
        "tret0-3": (df.ticker_ret_4h_pct>=0)&(df.ticker_ret_4h_pct<=3),
        "aligned": df.aligned==1,
    }
    import itertools
    print("Best 2-var stacks (N>=20, by +4h avg):")
    res=[]
    for a,b in itertools.combinations(feats,2):
        m=feats[a]&feats[b]; sub=df[m]
        if len(sub)>=20:
            d=desc(sub[R4]); res.append((f"{a} & {b}",d))
    for name,d in sorted(res,key=lambda x:-x[1]["avg"])[:12]:
        print(f"  {name:>26}: {fmt(d)}")
    print("\nBest 3-var stacks (N>=20, by +4h avg):")
    res=[]
    for a,b,c in itertools.combinations(feats,3):
        m=feats[a]&feats[b]&feats[c]; sub=df[m]
        if len(sub)>=20:
            d=desc(sub[R4]); res.append((f"{a} & {b} & {c}",d))
    for name,d in sorted(res,key=lambda x:-x[1]["avg"])[:12]:
        print(f"  {name:>34}: {fmt(d)}")

    # ---------- Regimes ----------
    hdr("O. REGIME ANALYSIS")
    print("Regime 1: BTC impulse (bre=1) vs non-impulse:")
    for v,lab in [(1,"impulse"),(0,"non-impulse")]:
        print(f"  {lab:>12}: {fmt(desc(df[df.btc_range_expansion==v][R4]))}")
    print(mw(df[df.btc_range_expansion==1][R4],df[df.btc_range_expansion==0][R4]))
    print("\nRegime 4: calendar window:")
    cat_report(df, "db", R4)
    # weekly
    df["week"]=df["alert_utc"].dt.isocalendar().week
    print("\nby ISO week (N>=10):")
    cat_report(df, "week", R4, min_n=10)

    # ---------- Exit intelligence ----------
    hdr("P. EXIT INTELLIGENCE")
    topq=df[df[R4]>2]
    print(f"Top quartile proxy (+4h>2%), N={len(topq)}:")
    print(f"  Exit A (+1h) avg: {topq[R1].mean():+.2f}%  total: {topq[R1].sum():+.1f}")
    print(f"  Exit B (+4h) avg: {topq[R4].mean():+.2f}%  total: {topq[R4].sum():+.1f}")
    print(f"  Exit C (+8h) avg: {topq[R8].mean():+.2f}%  total: {topq[R8].sum():+.1f}")
    print(f"  peak horizon dist among winners: {topq['peak_hz'].value_counts().sort_index().to_dict()}")
    # whole-portfolio exit comparison
    print(f"\nWhole dataset exit comparison (sum of signed returns):")
    for h in HORIZONS:
        print(f"  exit {h}: total={df[h].sum():+.1f}  avg={df[h].mean():+.3f}%  WR={(df[h]>0).mean()*100:.1f}%")
    print("\nTop 20 by +4h trajectory (1h/2h/4h/8h):")
    for _,r in df.nlargest(20,R4).iterrows():
        traj="fade" if r[R1]>r[R4] else "build"
        print(f"  {r['ticker']:>8} {str(r['date'])}: {r[R1]:+6.1f} {r[R2]:+6.1f} {r[R4]:+6.1f} {r[R8]:+6.1f}  [{traj}]")

    # ---------- Robustness / overfitting ----------
    hdr("Q. OVERFITTING DEFENSE")
    df_sorted=df.sort_values("alert_utc")
    half=len(df_sorted)//2
    h1,h2=df_sorted.iloc[:half],df_sorted.iloc[half:]
    print(f"Date split: h1 {h1['date'].min()}..{h1['date'].max()} (n={len(h1)}), "
          f"h2 {h2['date'].min()}..{h2['date'].max()} (n={len(h2)})")
    key_stacks = {
        "az>=3 & pctile>=75": (df.abs_az>=3)&(df.score_pctile>=75),
        "triple stack": (df.abs_az>=3)&(df.score_pctile>=75)&(df.cluster_size==1),
        "pctile>=75": df.score_pctile>=75,
        "cluster>=5 (NEG)": df.cluster_size>=5,
        "crypto_meme (NEG)": df.asset_class=="crypto_meme",
    }
    for name,m in key_stacks.items():
        full=desc(df[m][R4])
        s1=desc(h1[m.reindex(h1.index,fill_value=False)][R4])
        s2=desc(h2[m.reindex(h2.index,fill_value=False)][R4])
        print(f"\n  {name}:")
        print(f"    full: {fmt(full)}")
        print(f"    h1:   {fmt(s1)}")
        print(f"    h2:   {fmt(s2)}")
    # ticker-concentration check for triple stack
    stack=df[(df.abs_az>=3)&(df.score_pctile>=75)&(df.cluster_size==1)]
    print(f"\nTriple-stack ticker concentration: {stack['ticker'].value_counts().head(8).to_dict()}")
    print(f"Triple-stack top contributor to avg: ")
    contrib=stack.groupby("ticker")[R4].sum().sort_values(ascending=False)
    print("  ", contrib.head(5).round(1).to_dict())
    print(f"  stack avg with top ticker removed: ")
    for t in contrib.head(3).index:
        rem=stack[stack.ticker!=t]
        print(f"    minus {t}: {fmt(desc(rem[R4]))}")
    # standard error
    s=df[(df.abs_az>=3)&(df.score_pctile>=75)][R4]
    print(f"\naz>=3 & pctile>=75: avg={s.mean():+.3f}% SE={s.std()/np.sqrt(len(s)):.3f}% "
          f"t={s.mean()/(s.std()/np.sqrt(len(s))):.2f}")


if __name__ == "__main__":
    main()
