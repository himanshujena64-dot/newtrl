import io
import re
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="SAP MRP Engine",
    page_icon="⚙️",
    layout="wide",
)

st.title("⚙️ SAP MRP Engine — L1 to L4")
st.caption("Phantom handling · Alt-aware · NET propagation · Dynamic months")

with st.sidebar:
    st.header("Configuration")
    PHANTOM = st.text_input("Phantom Sp. Procurement code", value="50")
    VERIFY_L1 = st.text_input("Verify component L1", value="0010748460")
    VERIFY_L2 = st.text_input("Verify component L2", value="0010748458")
    VERIFY_L3 = st.text_input("Verify component L3 (phantom)", value="0010748814")
    VERIFY_L4 = st.text_input("Verify component L4", value="0010300601DEL")
    st.divider()
    st.subheader("Upload files")
    bom_file = st.file_uploader("BOM file (.xlsx)", type=["xlsx", "xls"], key="bom")
    req_file = st.file_uploader("Req and Stock (.xlsx)", type=["xlsx", "xls"], key="req")
    prod_file = st.file_uploader(
        "Production Orders (.xlsx) — optional",
        type=["xlsx", "xls"],
        key="prod",
    )
    run_btn = st.button("▶ Run MRP", type="primary", use_container_width=True)

MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_col_to_date(col):
    if isinstance(col, pd.Timestamp):
        return col.replace(day=1), col.strftime("%b-%y")
    if hasattr(col, "year") and hasattr(col, "month"):
        ts = pd.Timestamp(col)
        return ts.replace(day=1), ts.strftime("%b-%y")
    if pd.isna(col):
        return None
    s = str(col).strip()
    if not s:
        return None
    try:
        ts = pd.to_datetime(s, dayfirst=False, errors="raise")
        return ts.replace(day=1), ts.strftime("%b-%y")
    except Exception:
        pass
    m = re.match(r"([A-Za-z]{3})[-'\\s_](\\d{2,4})$", s)
    if m:
        mon_str, yr_str = m.group(1).lower(), m.group(2)
        mon_num = MONTH_ABBR.get(mon_str)
        if mon_num:
            yr = int(yr_str) + (2000 if len(yr_str) == 2 else 0)
            ts = pd.Timestamp(year=yr, month=mon_num, day=1)
            return ts, ts.strftime("%b-%y")
    return None


def standardize_req_header(v):
    if pd.isna(v):
        return ""
    s = str(v).strip()
    mapping = {"alt.": "Alt", "alternative": "Alt", "bom header": "BOM Header"}
    return mapping.get(s.lower(), s)


def detect_requirement_header_row(file_obj, sheet_name="Requirement", scan_rows=20):
    raw = pd.read_excel(file_obj, sheet_name=sheet_name, header=None, nrows=scan_rows, engine="openpyxl")
    best_row, best_score = 0, -1
    for i in range(len(raw)):
        cleaned = [standardize_req_header(x) for x in raw.iloc[i].tolist()]
        score = (
            (10 if "BOM Header" in cleaned else 0)
            + (5 if "Alt" in cleaned else 0)
            + sum(1 for x in cleaned if parse_col_to_date(x))
        )
        if score > best_score:
            best_score, best_row = score, i
    if best_score < 10:
        raise ValueError("Could not detect Requirement header row reliably.")
    return best_row


def is_phantom(val):
    return str(val).strip() == PHANTOM


def empty_prod_summary():
    return pd.DataFrame(columns=["Component", "Confirmed_Qty", "Open_Production_Qty"])


def get_ancestry_paths(component, bom):
    comp_rows = bom[bom["Component"] == component][
        [
            "BOM Header", "Alt", "Level", "Parent", "Component",
            "Required Qty", "Component descriptio", "Special procurement",
        ]
    ].drop_duplicates()

    paths = []
    for _, row in comp_rows.iterrows():
        path_comps = [row["Component"]]
        path_descs = [row["Component descriptio"]]
        path_qtys = [float(row["Required Qty"])]
        path_sp = [str(row["Special procurement"]).strip()]
        current = row["Parent"]
        fg = row["BOM Header"]
        alt = row["Alt"]

        for _ in range(4):
            if current == fg:
                break
            parent_row = bom[
                (bom["BOM Header"] == fg)
                & (bom["Alt"] == alt)
                & (bom["Component"] == current)
            ]
            if parent_row.empty:
                break
            pr = parent_row.iloc[0]
            path_comps.insert(0, pr["Component"])
            path_descs.insert(0, pr["Component descriptio"])
            path_qtys.insert(0, float(pr["Required Qty"]))
            path_sp.insert(0, str(pr["Special procurement"]).strip())
            current = pr["Parent"]

        paths.append({
            "fg": fg,
            "alt": str(alt),
            "level": int(row["Level"]),
            "path_comps": path_comps,
            "path_descs": path_descs,
            "path_qtys": path_qtys,
            "path_sp": path_sp,
        })
    return paths


def build_dot_tree(component, paths, req_df, result_dfs, months, stock, prod_summary):
    fg_demand = {}
    for path in paths:
        key = (path["fg"], path["alt"])
        rows = req_df[
            (req_df["BOM Header"] == path["fg"])
            & (req_df["Alt"] == path["alt"])
        ]
        total = rows[months].sum(numeric_only=True).sum() if not rows.empty else 0
        fg_demand[key] = float(total)

    gross_map = {}
    shortage_map = {}
    for df in result_dfs:
        if df is None or df.empty:
            continue
        agg = df.groupby("Component")[["Gross_Requirement", "Shortage"]].sum()
        for comp, row in agg.iterrows():
            gross_map[comp] = gross_map.get(comp, 0) + row["Gross_Requirement"]
            shortage_map[comp] = shortage_map.get(comp, 0) + row["Shortage"]

    def trunc(s, n=22):
        s = str(s)
        return (s[:n] + "…") if len(s) > n else s

    node_attrs = {}
    edges = []
    seen_edges = set()

    for path in paths:
        fg = path["fg"]
        alt = path["alt"]
        key = (fg, alt)
        demand = fg_demand.get(key, 0)

        fg_id = f"FG_{fg}_A{alt}".replace("-", "_")
        fg_label = f"FG\\n{fg}\\nAlt: {alt}\\nTotal demand: {demand:,.0f}"
        node_attrs[fg_id] = (
            f'label="{fg_label}" shape=box style="filled,rounded" '
            f'fillcolor="#4a90d9" fontcolor=white fontsize=11'
        )

        prev_id = fg_id

        for comp, desc, qty, sp in zip(
            path["path_comps"], path["path_descs"], path["path_qtys"], path["path_sp"]
        ):
            is_target = comp == component
            is_ph = sp == PHANTOM
            node_id = f"N_{comp}_FG_{fg}_A{alt}".replace("-", "_").replace(".", "_")

            gross = gross_map.get(comp, 0)
            shortage = shortage_map.get(comp, 0)
            stk = float(stock.get(comp, 0))

            if is_target:
                prod_row = prod_summary[prod_summary["Component"] == comp]
                conf = float(prod_row["Confirmed_Qty"].iloc[0]) if not prod_row.empty else 0
                open_qty = float(prod_row["Open_Production_Qty"].iloc[0]) if not prod_row.empty else 0
                label = (
                    f"{trunc(comp)}\\n{trunc(desc)}\\n"
                    f"Stock: {stk:,.0f}\\n"
                    f"Confirmed: {conf:,.0f}\\n"
                    f"Open PO: {open_qty:,.0f}\\n"
                    f"Gross req: {gross:,.0f}\\n"
                    f"Shortage: {shortage:,.0f}"
                )
                node_attrs[node_id] = (
                    f'label="{label}" shape=box style="filled,rounded" '
                    f'fillcolor="#27ae60" fontcolor=white fontsize=11 penwidth=2'
                )
            elif is_ph:
                label = (
                    f"PHANTOM\\n{trunc(comp)}\\n{trunc(desc)}\\n"
                    f"Sp.Proc: {sp}\\nQty: {qty:g}\\n(pass-through)"
                )
                node_attrs[node_id] = (
                    f'label="{label}" shape=box style="filled,dashed" '
                    f'fillcolor="#f5a623" fontcolor="#333" fontsize=10'
                )
            else:
                label = (
                    f"{trunc(comp)}\\n{trunc(desc)}\\n"
                    f"Qty: {qty:g}  Stock: {stk:,.0f}\\n"
                    f"Gross: {gross:,.0f}  Short: {shortage:,.0f}"
                )
                node_attrs[node_id] = (
                    f'label="{label}" shape=box style="filled,rounded" '
                    f'fillcolor="#f9e79f" fontcolor="#333" fontsize=10'
                )

            edge_key = (prev_id, node_id)
            if edge_key not in seen_edges:
                edges.append((prev_id, node_id, f"×{qty:g}"))
                seen_edges.add(edge_key)

            prev_id = node_id

    lines = [
        "digraph MRP {",
        "  rankdir=TB;",
        '  node [fontname="Arial"];',
        '  edge [fontname="Arial" fontsize=10];',
        '  graph [splines=ortho nodesep=0.5 ranksep=0.7];',
    ]
    for nid, attrs in node_attrs.items():
        lines.append(f'  "{nid}" [{attrs}];')
    for src, dst, lbl in edges:
        lines.append(f'  "{src}" -> "{dst}" [label="{lbl}"];')
    lines.append("}")
    return "\n".join(lines)


def show_search_section(bom, req_df, months, stock, prod_summary, result_l1, result_l2, result_l3, result_l4):
    st.divider()
    st.subheader("🔍 Component Search")
    st.caption("Search any component to see its full demand, shortage, production orders and BOM ancestry tree.")

    search_col, _ = st.columns([2, 3])
    with search_col:
        search_q = st.text_input(
            "Enter component code",
            placeholder="e.g. 0010748458",
            label_visibility="collapsed",
        )

    if not search_q:
        return

    comp = search_q.strip()
    result_dfs = [result_l1, result_l2, result_l3, result_l4]
    found_in = {}
    for lbl, df in zip(["L1", "L2", "L3", "L4"], result_dfs):
        if df is not None and not df.empty and comp in df["Component"].values:
            found_in[lbl] = df[df["Component"] == comp].copy()

    bom_in = bom[bom["Component"] == comp]
    if bom_in.empty and not found_in:
        st.warning(f"Component `{comp}` not found in BOM or MRP results.")
        return

    desc = bom_in["Component descriptio"].iloc[0] if not bom_in.empty else "—"
    ptype = bom_in["Procurement type"].iloc[0] if not bom_in.empty else "—"
    sp = bom_in["Special procurement"].iloc[0] if not bom_in.empty else "—"
    stk = float(stock.get(comp, 0))

    prod_row = prod_summary[prod_summary["Component"] == comp]
    conf_qty = float(prod_row["Confirmed_Qty"].iloc[0]) if not prod_row.empty else 0
    open_qty = float(prod_row["Open_Production_Qty"].iloc[0]) if not prod_row.empty else 0

    ph_badge = " 🔶 PHANTOM" if str(sp).strip() == PHANTOM else ""
    st.markdown(f"### `{comp}` — {desc}{ph_badge}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Stock on hand", f"{stk:,.3f}")
    c2.metric("Confirmed prod. qty", f"{conf_qty:,.0f}")
    c3.metric("Open production qty", f"{open_qty:,.0f}")
    c4.metric("Procurement type", ptype)
    c5.metric("Sp. procurement", sp if sp not in ("", "nan") else "—")

    if found_in:
        st.markdown("#### Monthly demand & shortage")
        all_rows = pd.concat(found_in.values(), ignore_index=True)
        monthly = all_rows.groupby("Month", as_index=False).agg(
            Gross_Requirement=("Gross_Requirement", "sum"),
            Stock_Used=("Stock_Used", "sum"),
            Shortage=("Shortage", "sum"),
            Stock_Remaining=("Stock_Remaining", "last"),
        )
        mo = {m: i for i, m in enumerate(months)}
        monthly["_order"] = monthly["Month"].map(mo)
        monthly = monthly.sort_values("_order").drop(columns="_order")
        monthly["Cumulative Shortage"] = monthly["Shortage"].cumsum()

        def highlight_shortage(row):
            colour = "background-color: #ffe0e0" if row["Shortage"] > 0 else ""
            return [colour] * len(row)

        st.dataframe(
            monthly.style.apply(highlight_shortage, axis=1).format({
                "Gross_Requirement": "{:,.2f}",
                "Stock_Used": "{:,.2f}",
                "Shortage": "{:,.2f}",
                "Stock_Remaining": "{:,.2f}",
                "Cumulative Shortage": "{:,.2f}",
            }),
            use_container_width=True,
            hide_index=True,
        )

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total gross req", f"{monthly['Gross_Requirement'].sum():,.2f}")
        s2.metric("Total stock consumed", f"{monthly['Stock_Used'].sum():,.2f}")
        s3.metric("Total shortage", f"{monthly['Shortage'].sum():,.2f}")
        s4.metric("Months with shortage", f"{(monthly['Shortage'] > 0).sum()} / {len(monthly)}")
    else:
        st.info("Component found in BOM but not in MRP results (no demand in requirement file or phantom).")

    st.markdown("#### BOM ancestry tree")
    st.caption("FG → intermediate levels → searched component (green). Phantom nodes shown with dashed border.")

    paths = get_ancestry_paths(comp, bom)
    if not paths:
        st.info("No ancestry paths found in BOM.")
        return

    st.markdown("**Finished goods that require this component:**")
    fg_rows = []
    for p in paths:
        rows = req_df[(req_df["BOM Header"] == p["fg"]) & (req_df["Alt"] == p["alt"])]
        total_demand = rows[months].sum(numeric_only=True).sum() if not rows.empty else 0
        monthly_vals = {m: float(rows[m].sum()) if not rows.empty else 0 for m in months}
        fg_rows.append({
            "FG code": p["fg"],
            "Alt": p["alt"],
            "BOM level": p["level"],
            "Path depth": len(p["path_comps"]),
            "Total demand": f"{total_demand:,.0f}",
            **{m: f"{monthly_vals[m]:,.0f}" for m in months},
        })

    fg_df = pd.DataFrame(fg_rows).drop_duplicates(subset=["FG code", "Alt"])
    st.dataframe(fg_df, use_container_width=True, hide_index=True)

    max_paths = 12
    display_paths = paths[:max_paths]
    if len(paths) > max_paths:
        st.caption(f"⚠️ Showing {max_paths} of {len(paths)} ancestry paths for readability.")

    dot_src = build_dot_tree(comp, display_paths, req_df, result_dfs, months, stock, prod_summary)
    try:
        st.graphviz_chart(dot_src, use_container_width=True)
    except Exception as e:
        st.error(f"Tree render error: {e}")
        with st.expander("DOT source (debug)"):
            st.code(dot_src, language="dot")


def run_mrp(bom_file, req_file, prod_file):
    logs = []
    log = lambda msg: logs.append(msg)
    status = st.status("Running MRP engine ...", expanded=True)

    with status:
        st.write("► Building clean BOM ...")

    bom = pd.read_excel(bom_file, engine="openpyxl")
    bom.columns = bom.columns.str.strip()
    if "Alt." in bom.columns:
        bom = bom.rename(columns={"Alt.": "Alt"})

    bom["Level"] = pd.to_numeric(bom["Level"], errors="coerce").fillna(0).astype(int)
    bom = bom.reset_index(drop=True)

    parents, stack = [], {}
    for i in range(len(bom)):
        lvl = bom.loc[i, "Level"]
        parent = bom.loc[i, "BOM Header"] if lvl == 1 else stack.get(lvl - 1)
        stack = {k: v for k, v in stack.items() if k <= lvl}
        stack[lvl] = bom.loc[i, "Component"]
        parents.append(parent)
    bom["Parent"] = parents

    drop_cols = [
        "Plant", "Usage", "Quantity", "Unit", "BOM L/T", "BOM code", "Item",
        "Mat. Group", "Mat. Group Desc.", "Pur. Group", "Pur. Group Desc.",
        "MRP Controller", "MRP Controller Desc.",
    ]
    bom = bom.drop(columns=[c for c in drop_cols if c in bom.columns], errors="ignore")

    for old, new in [
        ("Component description", "Component descriptio"),
        ("BOM header description", "BOM header descripti"),
    ]:
        if old in bom.columns:
            bom = bom.rename(columns={old: new})

    keep = [
        "BOM Header", "BOM header descripti", "Alt", "Level", "Path", "Parent",
        "Component", "Component descriptio", "Required Qty", "Base unit",
        "Procurement type", "Special procurement",
    ]

    missing_bom = [c for c in ["BOM Header", "Level", "Component", "Required Qty"] if c not in bom.columns]
    if missing_bom:
        st.error(f"Missing required BOM columns: {missing_bom}")
        return None

    bom = bom[[c for c in keep if c in bom.columns]].copy()
    for col, default in [("Alt", "0"), ("Special procurement", ""), ("Procurement type", ""), ("Component descriptio", "")]:
        if col not in bom.columns:
            bom[col] = default

    bom["Component"] = bom["Component"].astype(str).str.strip()
    bom["BOM Header"] = bom["BOM Header"].astype(str).str.strip()
    bom["Special procurement"] = bom["Special procurement"].astype(str).str.strip()
    bom["Procurement type"] = bom["Procurement type"].astype(str).str.strip()
    bom["Component descriptio"] = bom["Component descriptio"].astype(str).str.strip()
    bom["Required Qty"] = pd.to_numeric(bom["Required Qty"], errors="coerce").fillna(0)
    bom["Alt"] = pd.to_numeric(bom["Alt"], errors="coerce").fillna(0).astype(int).astype(str)

    log(f"BOM rows: {len(bom):,}  |  Unique headers: {bom['BOM Header'].nunique()}")

    with status:
        st.write("► Loading Requirement and Stock ...")

    req_header_row = detect_requirement_header_row(req_file, sheet_name="Requirement")
    req_file.seek(0)
    req = pd.read_excel(req_file, sheet_name="Requirement", header=None, engine="openpyxl")
    req.columns = [standardize_req_header(x) for x in req.iloc[req_header_row].tolist()]
    req = req.iloc[req_header_row + 1:].reset_index(drop=True)
    req = req.loc[:, [str(c).strip() != "" for c in req.columns]]
    req = req.loc[:, ~pd.Index(req.columns).duplicated(keep="first")]

    missing_req = [c for c in ["BOM Header", "Alt"] if c not in req.columns]
    if missing_req:
        st.error(f"Missing Requirement columns: {missing_req}. Found: {req.columns.tolist()}")
        return None

    req["BOM Header"] = req["BOM Header"].astype(str).str.strip()
    req["Alt"] = pd.to_numeric(req["Alt"], errors="coerce").fillna(0).astype(int).astype(str)

    non_month_cols = {"BOM Header", "Alt"}
    parsed = []
    for col in req.columns:
        if col in non_month_cols:
            continue
        result = parse_col_to_date(col)
        if result:
            ts, label = result
            parsed.append({"orig": col, "ts": ts, "label": label})
    parsed.sort(key=lambda x: x["ts"])

    rename_map = {p["orig"]: p["label"] for p in parsed if p["orig"] != p["label"]}
    if rename_map:
        req = req.rename(columns=rename_map)

    months = [p["label"] for p in parsed]
    month_order = {m: i for i, m in enumerate(months)}

    if not months:
        st.error(f"No month columns detected. Found: {req.columns.tolist()}")
        return None

    for m in months:
        req[m] = pd.to_numeric(req[m].astype(str).str.replace(",", "", regex=False).str.strip(), errors="coerce").fillna(0)

    req_file.seek(0)
    stock_raw = pd.read_excel(
        req_file,
        sheet_name="Stock",
        usecols=[0, 1],
        header=0,
        names=["Component", "Stock_Qty"],
        engine="openpyxl",
    )
    stock_raw = stock_raw.dropna(subset=["Component"]).copy()
    stock_raw["Component"] = stock_raw["Component"].astype(str).str.strip()
    stock_raw["Stock_Qty"] = pd.to_numeric(
        stock_raw["Stock_Qty"].astype(str).str.replace(",", "", regex=False).str.strip(),
        errors="coerce",
    ).fillna(0)
    stock = stock_raw.groupby("Component")["Stock_Qty"].sum()

    req_long = req.melt(id_vars=["BOM Header", "Alt"], value_vars=months, var_name="Month", value_name="FG_Demand")
    req_long = req_long[req_long["FG_Demand"] > 0].copy()

    log(f"Months ({len(months)}): {months}")
    log(f"Requirement rows (non-zero): {len(req_long):,}")
    log(f"Stock components: {len(stock):,}")

    with status:
        st.write("► Loading Production Orders ...")

    prod_summary = empty_prod_summary()
    if prod_file is not None:
        try:
            coois = pd.read_excel(prod_file, engine="openpyxl")
            coois.columns = coois.columns.str.strip()
            if not coois.empty:
                status_col = next((c for c in coois.columns if "status" in c.lower()), None)
                mat_col = next((c for c in coois.columns if "material" in c.lower() and "description" not in c.lower()), None)
                ord_col = next((c for c in coois.columns if "order" in c.lower() and ("qty" in c.lower() or "quantity" in c.lower())), None)
                del_col = next((c for c in coois.columns if "deliver" in c.lower() or ("quantity" in c.lower() and "gr" in c.lower())), None)
                conf_col = next((c for c in coois.columns if "confirm" in c.lower() and "quantity" in c.lower()), None)

                if all([status_col, mat_col, ord_col, del_col, conf_col]):
                    coois = coois[~coois[status_col].astype(str).str.contains("TECO", case=False, na=False)].copy()
                    coois[mat_col] = coois[mat_col].astype(str).str.strip()
                    coois[ord_col] = pd.to_numeric(coois[ord_col], errors="coerce").fillna(0)
                    coois[del_col] = pd.to_numeric(coois[del_col], errors="coerce").fillna(0)
                    coois[conf_col] = pd.to_numeric(coois[conf_col], errors="coerce").fillna(0)
                    coois["Open_Qty"] = (coois[ord_col] - coois[del_col]).clip(lower=0)
                    prod_summary = (
                        coois.groupby(mat_col, as_index=False)
                        .agg(Confirmed_Qty=(conf_col, "sum"), Open_Production_Qty=("Open_Qty", "sum"))
                        .rename(columns={mat_col: "Component"})
                    )
                    prod_summary["Component"] = prod_summary["Component"].astype(str).str.strip()
                    log(f"Production order rows (non-TECO): {len(coois):,}")
                else:
                    log("Production order columns not detected — defaulting to 0.")
        except Exception as e:
            log(f"Production order error ({e}) — defaulting to 0.")

    def get_sfrac(rows, comp_col, gross_col):
        agg = rows.groupby([comp_col, "Month", "Month_Order"], as_index=False)[gross_col].sum()
        sfrac = {}
        for comp, grp in agg.groupby(comp_col):
            avail = float(stock.get(comp, 0))
            for _, row in grp.sort_values("Month_Order").iterrows():
                g = float(row[gross_col])
                sfrac[(comp, row["Month"])] = max(0.0, g - avail) / g if g > 0 else 0.0
                avail = max(0.0, avail - g)
        return sfrac

    def make_report(gross_agg_df, comp_col):
        base_cols = ["Component", "Description", "Month", "Gross_Requirement", "Stock_Used", "Shortage", "Stock_Remaining"]
        if gross_agg_df.empty:
            return pd.DataFrame(columns=base_cols)
        results = []
        for comp, grp in gross_agg_df.groupby(comp_col):
            avail = float(stock.get(comp, 0))
            desc = grp["Desc"].iloc[0]
            for _, row in grp.sort_values("Month_Order").iterrows():
                gr = float(row["Gross"])
                consumed = min(avail, gr)
                shortage = max(0.0, gr - avail)
                avail = max(0.0, avail - gr)
                results.append({
                    "Component": comp,
                    "Description": desc,
                    "Month": row["Month"],
                    "Gross_Requirement": gr,
                    "Stock_Used": consumed,
                    "Shortage": shortage,
                    "Stock_Remaining": avail,
                })
        return pd.DataFrame(results, columns=base_cols)

    def apply_sfrac(df, gross_col, ph_col, sfrac_dict, comp_col):
        return df.apply(
            lambda r: r[gross_col] if is_phantom(r[ph_col]) else r[gross_col] * sfrac_dict.get((r[comp_col], r["Month"]), 1.0),
            axis=1,
        )

    with status:
        st.write("► Running MRP explosion ...")

    bom_l1 = (
        bom[bom["Level"] == 1][["BOM Header", "Alt", "Component", "Component descriptio", "Required Qty", "Special procurement"]]
        .copy()
        .rename(columns={
            "Component": "L1_Comp",
            "Component descriptio": "L1_Desc",
            "Required Qty": "L1_Qty",
            "Special procurement": "L1_Ph",
        })
    )
    l1 = req_long.merge(bom_l1, on=["BOM Header", "Alt"], how="inner")
    l1["L1_Gross"] = l1["FG_Demand"] * l1["L1_Qty"]
    l1["Month_Order"] = l1["Month"].map(month_order)
    l1_norm = l1[~l1["L1_Ph"].apply(is_phantom)].copy()
    l1_sfrac = get_sfrac(l1_norm, "L1_Comp", "L1_Gross")
    l1["L1_Eff"] = apply_sfrac(l1, "L1_Gross", "L1_Ph", l1_sfrac, "L1_Comp")
    l1_agg = (
        l1_norm.groupby(["L1_Comp", "L1_Desc", "Month", "Month_Order"], as_index=False)["L1_Gross"].sum()
        .rename(columns={"L1_Comp": "Component", "L1_Desc": "Desc", "L1_Gross": "Gross"})
    )
    result_l1 = make_report(l1_agg, "Component")

    bom_l2 = (
        bom[bom["Level"] == 2][["BOM Header", "Alt", "Parent", "Component", "Component descriptio", "Required Qty", "Special procurement"]]
        .copy()
        .rename(columns={
            "Parent": "L1_Comp",
            "Component": "L2_Comp",
            "Component descriptio": "L2_Desc",
            "Required Qty": "L2_Qty",
            "Special procurement": "L2_Ph",
        })
    )
    l2 = l1.merge(bom_l2, on=["BOM Header", "Alt", "L1_Comp"], how="inner")
    l2["L2_Gross"] = l2["L1_Eff"] * l2["L2_Qty"]
    l2_norm = l2[~l2["L2_Ph"].apply(is_phantom)].copy()
    l2_sfrac = get_sfrac(l2_norm, "L2_Comp", "L2_Gross")
    l2["L2_Eff"] = apply_sfrac(l2, "L2_Gross", "L2_Ph", l2_sfrac, "L2_Comp")
    l2_agg = (
        l2_norm.groupby(["L2_Comp", "L2_Desc", "Month", "Month_Order"], as_index=False)["L2_Gross"].sum()
        .rename(columns={"L2_Comp": "Component", "L2_Desc": "Desc", "L2_Gross": "Gross"})
    )
    result_l2 = make_report(l2_agg, "Component")

    bom_l3 = (
        bom[bom["Level"] == 3][["BOM Header", "Alt", "Parent", "Component", "Component descriptio", "Required Qty", "Special procurement"]]
        .copy()
        .rename(columns={
            "Parent": "L2_Comp",
            "Component": "L3_Comp",
            "Component descriptio": "L3_Desc",
            "Required Qty": "L3_Qty",
            "Special procurement": "L3_Ph",
        })
    )
    l3 = l2.merge(bom_l3, on=["BOM Header", "Alt", "L2_Comp"], how="inner")
    l3["L3_Gross"] = l3.apply(lambda r: r["L2_Eff"] if is_phantom(r["L3_Ph"]) else r["L2_Eff"] * r["L3_Qty"], axis=1)
    l3_norm = l3[~l3["L3_Ph"].apply(is_phantom)].copy()
    l3_sfrac = get_sfrac(l3_norm, "L3_Comp", "L3_Gross")
    l3["L3_Eff"] = apply_sfrac(l3, "L3_Gross", "L3_Ph", l3_sfrac, "L3_Comp")
    l3_agg = (
        l3_norm.groupby(["L3_Comp", "L3_Desc", "Month", "Month_Order"], as_index=False)["L3_Gross"].sum()
        .rename(columns={"L3_Comp": "Component", "L3_Desc": "Desc", "L3_Gross": "Gross"})
    )
    result_l3 = make_report(l3_agg, "Component")

    bom_l4 = (
        bom[bom["Level"] == 4][["BOM Header", "Alt", "Parent", "Component", "Component descriptio", "Required Qty", "Special procurement"]]
        .copy()
        .rename(columns={
            "Parent": "L3_Comp",
            "Component": "L4_Comp",
            "Component descriptio": "L4_Desc",
            "Required Qty": "L4_Qty",
            "Special procurement": "L4_Ph",
        })
    )
    l4 = l3.merge(bom_l4, on=["BOM Header", "Alt", "L3_Comp"], how="inner")
    l4["L4_Gross"] = l4["L3_Eff"] * l4["L4_Qty"]
    l4_agg = (
        l4.groupby(["L4_Comp", "L4_Desc", "Month", "Month_Order"], as_index=False)["L4_Gross"].sum()
        .rename(columns={"L4_Comp": "Component", "L4_Desc": "Desc", "L4_Gross": "Gross"})
    )
    result_l4 = make_report(l4_agg, "Component")

    with status:
        st.write("► Building output ...")
    status.update(label="MRP complete ✅", state="complete", expanded=False)

    st.divider()
    st.subheader("📊 Summary")
    col1, col2, col3, col4 = st.columns(4)
    for col_ui, lbl, df in zip([col1, col2, col3, col4], ["L1", "L2", "L3", "L4"], [result_l1, result_l2, result_l3, result_l4]):
        short = df[df["Shortage"] > 0]["Component"].nunique() if not df.empty else 0
        with col_ui:
            st.metric(f"{lbl} components", df["Component"].nunique() if not df.empty else 0)
            st.metric("With shortage", short)

    st.divider()
    st.subheader("🔍 Verification")
    tab1, tab2, tab3, tab4 = st.tabs(["L1", "L2", "L3 (phantom check)", "L4"])

    def show_verify(tab, result_df, target, level_label):
        with tab:
            st.markdown(f"**{level_label} — `{target}`**")
            if result_df is None or result_df.empty:
                st.warning("No rows at this level.")
                return
            t = result_df[result_df["Component"] == target]
            if t.empty:
                st.info("Not found — phantom or no demand at this level.")
                return
            st.caption(f"Description: {t['Description'].iloc[0]} | Opening Stock: {stock.get(target, 0):,.3f}")
            st.dataframe(
                t[["Month", "Gross_Requirement", "Stock_Used", "Shortage", "Stock_Remaining"]].reset_index(drop=True),
                use_container_width=True,
            )

    show_verify(tab1, result_l1, VERIFY_L1, "LEVEL 1")
    show_verify(tab2, result_l2, VERIFY_L2, "LEVEL 2")

    with tab3:
        phantom_found = (not result_l3.empty) and (VERIFY_L3 in result_l3["Component"].values)
        if phantom_found:
            st.error(f"ERROR: {VERIFY_L3} found in results — phantom logic broken!")
        else:
            st.success(f"✅ {VERIFY_L3} correctly SKIPPED.")
        st.caption(f"Phantom rows passed through to L4: {len(l3[l3['L3_Ph'].apply(is_phantom)]):,}")

    show_verify(tab4, result_l4, VERIFY_L4, "LEVEL 4")

    if months:
        l4_jan = l4[(l4["L4_Comp"] == VERIFY_L4) & (l4["Month"] == months[0])]
        if not l4_jan.empty:
            with tab4:
                breakdown = l4_jan.groupby("BOM Header")["L4_Gross"].sum().sort_values(ascending=False).reset_index()
                breakdown.columns = ["BOM Header", f"Gross ({months[0]})"]
                total = breakdown[f"Gross ({months[0]})"].sum()
                stk = stock.get(VERIFY_L4, 0)
                st.caption(f"Total: {total:,.3f} | Stock: {stk:,.3f} | Shortage: {max(0, total - stk):,.3f}")
                st.dataframe(breakdown, use_container_width=True)

    final_output = pd.concat([result_l1, result_l2, result_l3, result_l4], ignore_index=True)
    all_comps = final_output[["Component", "Description"]].drop_duplicates(subset="Component").copy()

    pivot = (
        final_output.pivot_table(index=["Component", "Description"], columns="Month", values="Shortage", aggfunc="sum", fill_value=0)
        .reset_index()
    )
    pivot = all_comps.merge(pivot, on=["Component", "Description"], how="left").fillna(0)

    month_cols = [m for m in months if m in pivot.columns]
    if month_cols:
        pivot[month_cols] = pivot[month_cols].cumsum(axis=1)

    bom_master = bom[["Component", "Procurement type", "Special procurement"]].drop_duplicates(subset="Component")
    stock_df = stock.reset_index().rename(columns={"Stock_Qty": "Stock"})

    pivot = (
        pivot.merge(bom_master, on="Component", how="left")
        .merge(stock_df, on="Component", how="left")
        .merge(prod_summary, on="Component", how="left")
    )

    pivot["Procurement type"] = pivot["Procurement type"].fillna("")
    pivot["Special procurement"] = pivot["Special procurement"].fillna("")
    pivot["Stock"] = pivot["Stock"].fillna(0)
    pivot["Confirmed_Qty"] = pivot["Confirmed_Qty"].fillna(0)
    pivot["Open_Production_Qty"] = pivot["Open_Production_Qty"].fillna(0)

    pivot = pivot.rename(columns={"Description": "Component descri"})
    final_cols = [
        "Component", "Component descri", "Procurement type", "Special procurement",
        "Confirmed_Qty", "Open_Production_Qty", "Stock",
    ] + month_cols
    for c in final_cols:
        if c not in pivot.columns:
            pivot[c] = 0 if c in month_cols + ["Confirmed_Qty", "Open_Production_Qty", "Stock"] else ""
    pivot = pivot[final_cols].sort_values("Component").reset_index(drop=True)

    st.divider()
    st.subheader("📋 Output preview")
    st.dataframe(pivot.head(200), use_container_width=True)
    st.caption(f"{len(pivot):,} rows · {len(month_cols)} months · cumulative shortage")

    buf = io.BytesIO()
    pivot.to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    st.download_button(
        label="⬇️ Download mrp_final.xlsx",
        data=buf,
        file_name="mrp_final.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )

    with st.expander("Run log"):
        for line in logs:
            st.text(line)

    return {
        "bom": bom,
        "req": req,
        "months": months,
        "stock": stock,
        "prod_summary": prod_summary,
        "result_l1": result_l1,
        "result_l2": result_l2,
        "result_l3": result_l3,
        "result_l4": result_l4,
    }


if "mrp_results" not in st.session_state:
    st.session_state["mrp_results"] = None

if not run_btn and bom_file is None:
    st.info("Upload your files in the sidebar, then click **▶ Run MRP**.")
elif run_btn:
    if bom_file is None or req_file is None:
        st.warning("Please upload at least the BOM file and the Req & Stock file.")
    else:
        try:
            results = run_mrp(bom_file, req_file, prod_file)
            if results is not None:
                st.session_state["mrp_results"] = results
        except ImportError as e:
            st.error(f"Dependency error: {e}. Please ensure openpyxl is installed in requirements.txt.")
        except Exception as e:
            st.exception(e)

if st.session_state["mrp_results"] is not None:
    r = st.session_state["mrp_results"]
    try:
        show_search_section(
            bom=r["bom"],
            req_df=r["req"],
            months=r["months"],
            stock=r["stock"],
            prod_summary=r["prod_summary"],
            result_l1=r["result_l1"],
            result_l2=r["result_l2"],
            result_l3=r["result_l3"],
            result_l4=r["result_l4"],
        )
    except Exception as e:
        st.error(f"Search error: {e}")
