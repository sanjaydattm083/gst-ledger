import streamlit as st
import pandas as pd
import json
import os
import re
import requests
import base64
from datetime import datetime, date
from io import BytesIO

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

HOME_STATE = "Madhya Pradesh"
DATA_FILE = "bansal_ledger_data.csv"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

def get_api_key():
    try:
        return st.secrets["gemini"]["api_key"]
    except:
        return ""

STATES = [
    "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat",
    "Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh",
    "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan",
    "Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal",
    "Delhi","Jammu & Kashmir","Ladakh","Chandigarh","Puducherry"
]

STATE_CODES = {
    "01":"Jammu & Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh",
    "05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan","09":"Uttar Pradesh",
    "10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur",
    "15":"Mizoram","16":"Tripura","17":"Meghalaya","18":"Assam","19":"West Bengal",
    "20":"Jharkhand","21":"Odisha","22":"Chhattisgarh","23":"Madhya Pradesh",
    "24":"Gujarat","27":"Maharashtra","29":"Karnataka","30":"Goa","32":"Kerala",
    "33":"Tamil Nadu","34":"Puducherry","36":"Telangana","37":"Andhra Pradesh",
}

GST_INFO = [
    {"hsn":"5208-5212","desc":"Woven cotton fabrics","rate":5},
    {"hsn":"52 (raw)","desc":"Raw cotton","rate":5},
    {"hsn":"551311","desc":"Teery/Rubiya synthetic blend","rate":5},
    {"hsn":"6101-6117","desc":"Knitted apparel","rate":12},
    {"hsn":"6201-6217","desc":"Woven apparel & clothing","rate":12},
    {"hsn":"6301-6310","desc":"Blankets, bed linen, curtains","rate":12},
]

DEADLINES = [
    {"form":"GSTR-1","desc":"Outward supplies return","day":11,"monthly":True},
    {"form":"GSTR-3B","desc":"Summary return","day":20,"monthly":True},
    {"form":"GSTR-9","desc":"Annual return","month":12,"day":31,"monthly":False},
]

FY_LIST = ["2025-26","2024-25","2023-24","2022-23"]
QUARTERS = [
    {"id":"Q1","label":"Q1 (Apr-Jun)"},
    {"id":"Q2","label":"Q2 (Jul-Sep)"},
    {"id":"Q3","label":"Q3 (Oct-Dec)"},
    {"id":"Q4","label":"Q4 (Jan-Mar)"},
]

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def decode_gstin(gstin):
    if not gstin or len(gstin) != 15:
        return None
    sc = gstin[:2]
    state = STATE_CODES.get(sc, "")
    p = gstin[5]
    types = {"P":"Proprietorship","C":"Company","F":"Firm/LLP","H":"HUF","T":"Trust"}
    return {"state":state,"stateCode":sc,"entityType":types.get(p,"Unknown"),"pan":gstin[2:12]}

def get_quarter(d):
    m = d.month
    if 4<=m<=6: return "Q1"
    if 7<=m<=9: return "Q2"
    if 10<=m<=12: return "Q3"
    return "Q4"

def get_fy(d):
    m, y = d.month, d.year
    if m >= 4: return f"{y}-{str(y+1)[2:]}"
    return f"{y-1}-{str(y)[2:]}"

def calc_tax(amount, gst_rate, party_state, transport):
    amt = float(amount or 0)
    rate = float(gst_rate or 5)
    fare = float(transport or 0)
    inter = bool(party_state and party_state != HOME_STATE)
    cgst_r = 0 if inter else rate/2
    sgst_r = 0 if inter else rate/2
    igst_r = rate if inter else 0
    cgst = round(amt*cgst_r/100, 2)
    sgst = round(amt*sgst_r/100, 2)
    igst = round(amt*igst_r/100, 2)
    grand = round(amt+cgst+sgst+igst+fare, 2)
    return {"inter":inter,"cgst_r":cgst_r,"sgst_r":sgst_r,"igst_r":igst_r,
            "cgst":cgst,"sgst":sgst,"igst":igst,"transport":fare,"grand":grand}

def get_alerts():
    now = datetime.now()
    out = []
    for d in DEADLINES:
        if d["monthly"]:
            dl = datetime(now.year, now.month, d["day"])
            if dl < now:
                nm = now.month+1 if now.month<12 else 1
                ny = now.year if now.month<12 else now.year+1
                dl = datetime(ny, nm, d["day"])
        else:
            dl = datetime(now.year, d["month"], d["day"])
            if dl < now: dl = datetime(now.year+1, d["month"], d["day"])
        days = (dl-now).days+1
        out.append({**d,"days":days,"date_str":dl.strftime("%d-%m-%Y"),"urgent":days<=7})
    return out

def load_data():
    if os.path.exists(DATA_FILE):
        df = pd.read_csv(DATA_FILE)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        return df
    return pd.DataFrame(columns=[
        "date","party_name","gstin","invoice_id","taxable_amount",
        "gst_rate","cgst_r","cgst","sgst_r","sgst","igst_r","igst",
        "transport","grand_total","city","state","pin_code",
        "quarter","fy","inter_state"
    ])

def save_data(df):
    df.to_csv(DATA_FILE, index=False)

# ──────────────────────────────────────────────
# GEMINI OCR via REST API
# ──────────────────────────────────────────────

EXTRACT_PROMPT = """You are an Indian GST invoice data extractor for a textile/clothing business.
Extract fields from this invoice. Return ONLY valid JSON, no markdown backticks:
{"partyName":"seller business name","gstin":"seller 15-digit GSTIN","amount":"taxable amount as number","invoiceId":"invoice number","date":"YYYY-MM-DD","city":"seller city","state":"seller Indian state full name","pinCode":"6 digit PIN","gstRate":"5 or 12 or 18","transport":"freight charge number or 0","cgst":"CGST amount or 0","sgst":"SGST amount or 0","igst":"IGST amount or 0","grandTotal":"net payable amount as number"}
Use "" for missing strings, 0 for missing numbers. Return ONLY JSON."""

def extract_with_gemini(file_bytes, mime_type, api_key):
    """Call Gemini REST API to extract invoice data."""
    b64_data = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "contents": [{
            "parts": [
                {"text": EXTRACT_PROMPT},
                {"inline_data": {"mime_type": mime_type, "data": b64_data}}
            ]
        }]
    }

    resp = requests.post(
        f"{GEMINI_URL}?key={api_key}",
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=60
    )

    if resp.status_code != 200:
        raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    return json.loads(text.strip())

# ──────────────────────────────────────────────
# STREAMLIT APP
# ──────────────────────────────────────────────

st.set_page_config(page_title="Bunser Textiles GST Ledger", page_icon="🧵", layout="centered")

st.markdown("""
<style>
    .block-container { max-width: 750px; padding-top: 1.2rem; }
    .entry-card {
        background: #141420; border: 1px solid #2a2a3a; border-radius: 10px;
        padding: 14px; margin-bottom: 8px;
    }
    .alert-box {
        border-radius: 8px; padding: 10px 14px; margin-bottom: 6px;
    }
    div[data-testid="stMetric"] {
        background: #141420; border: 1px solid #2a2a3a;
        border-radius: 10px; padding: 10px;
    }
</style>
""", unsafe_allow_html=True)

# Header
c1, c2 = st.columns([1, 8])
with c1:
    st.markdown("""<div style="width:45px;height:45px;background:linear-gradient(135deg,#7c3aed,#a78bfa);
    border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:22px;
    font-weight:800;color:#fff;margin-top:8px;">B</div>""", unsafe_allow_html=True)
with c2:
    st.markdown("## Bunser Textiles")
    st.caption("Huntergger Main Shop · GST Ledger")

if "df" not in st.session_state:
    st.session_state.df = load_data()

# Alert strip
alerts = get_alerts()
urgent = [a for a in alerts if a["urgent"]]
if urgent:
    st.error(" | ".join([f"⚠️ **{a['form']}**: {a['days']}d left" for a in urgent]))

# Tabs
tab_scan, tab_entry, tab_ledger, tab_gst, tab_alerts = st.tabs([
    "📷 Scan Invoice", "📝 Manual Entry", "📒 Ledger", "📊 GST Rates", "🔔 Alerts"
])

# ──────────── SCAN TAB ────────────
with tab_scan:
    st.subheader("Scan Invoice with Gemini AI")
    st.caption("Upload PDF or photo. AI extracts all details automatically.")

    key_from_secrets = get_api_key()
    if key_from_secrets:
        api_key = key_from_secrets
        st.success("✅ API key loaded from secrets.toml")
    else:
        api_key = st.text_input("Gemini API Key", type="password",
                                help="Get free key: https://aistudio.google.com/apikey")

    uploaded = st.file_uploader("Upload Invoice", type=["pdf","png","jpg","jpeg","webp"], key="scan_upload")

    if uploaded and api_key:
        file_bytes = uploaded.read()
        mime = uploaded.type or "image/jpeg"

        # Preview
        if "image" in mime:
            st.image(file_bytes, caption="Uploaded Invoice", use_container_width=True)
        else:
            st.info(f"📄 PDF uploaded: {uploaded.name} ({len(file_bytes)//1024} KB)")

        if st.button("🤖 Extract with Gemini AI", use_container_width=True, type="primary"):
            with st.spinner("AI is reading your invoice..."):
                try:
                    result = extract_with_gemini(file_bytes, mime, api_key)
                    st.session_state.ocr = result
                    st.success("✅ Data extracted!")
                except Exception as e:
                    st.error(f"❌ Failed: {e}")
                    st.session_state.ocr = None

    if st.session_state.get("ocr"):
        r = st.session_state.ocr
        st.markdown("---")
        st.markdown("#### Verify Extracted Data")

        c1, c2 = st.columns(2)
        with c1:
            try: ocr_date = datetime.strptime(r.get("date",""), "%Y-%m-%d").date()
            except: ocr_date = date.today()
            s_date = st.date_input("Date", value=ocr_date, key="sd")
            s_party = st.text_input("Party Name", value=r.get("partyName",""), key="sp")
            s_amt = st.text_input("Taxable Amount", value=str(r.get("amount","")), key="sa")
            s_city = st.text_input("City", value=r.get("city",""), key="sc")
        with c2:
            s_gstin = st.text_input("GSTIN", value=r.get("gstin",""), key="sg", max_chars=15)
            s_inv = st.text_input("Invoice ID", value=str(r.get("invoiceId","")), key="si")
            gr = int(r.get("gstRate",5))
            s_rate = st.selectbox("GST Rate", [5,12,18], index=[5,12,18].index(gr) if gr in [5,12,18] else 0, key="sr")
            rs = r.get("state","")
            s_state = st.selectbox("State", [""]+STATES, index=(STATES.index(rs)+1) if rs in STATES else 0, key="ss")

        s_tr = st.number_input("Transport/Fare", value=float(r.get("transport",0)), min_value=0.0, key="str")
        s_pin = st.text_input("PIN Code", value=str(r.get("pinCode","")), max_chars=6, key="spin")

        if s_gstin and len(s_gstin)==15:
            info = decode_gstin(s_gstin.upper())
            if info:
                st.info(f"🔍 **{info['state']}** | {info['entityType']} | PAN: `{info['pan']}`")

        try:
            tax = calc_tax(s_amt, s_rate, s_state, s_tr)
            tc = st.columns(4)
            if tax["inter"]:
                tc[0].metric("IGST", f"₹{tax['igst']}", f"@{tax['igst_r']}%")
            else:
                tc[0].metric("CGST", f"₹{tax['cgst']}", f"@{tax['cgst_r']}%")
                tc[1].metric("SGST", f"₹{tax['sgst']}", f"@{tax['sgst_r']}%")
            tc[2].metric("Transport", f"₹{tax['transport']}")
            tc[3].metric("Grand Total", f"₹{tax['grand']}")
            st.caption(f"{'Inter-State (IGST)' if tax['inter'] else 'Intra-State (CGST+SGST)'}")
        except:
            tax = None

        if st.button("✅ Save Entry", use_container_width=True, type="primary", key="ssave"):
            if not s_party or not s_gstin or not s_amt or not s_inv:
                st.warning("Fill all required fields")
            elif len(s_gstin)!=15:
                st.warning("GSTIN must be 15 characters")
            else:
                tax = calc_tax(s_amt, s_rate, s_state, s_tr)
                row = pd.DataFrame([{
                    "date":s_date,"party_name":s_party,"gstin":s_gstin.upper(),
                    "invoice_id":s_inv,"taxable_amount":float(s_amt),
                    "gst_rate":s_rate,"cgst_r":tax["cgst_r"],"cgst":tax["cgst"],
                    "sgst_r":tax["sgst_r"],"sgst":tax["sgst"],"igst_r":tax["igst_r"],
                    "igst":tax["igst"],"transport":tax["transport"],"grand_total":tax["grand"],
                    "city":s_city,"state":s_state,"pin_code":s_pin,
                    "quarter":get_quarter(s_date),"fy":get_fy(s_date),"inter_state":tax["inter"]
                }])
                st.session_state.df = pd.concat([st.session_state.df, row], ignore_index=True)
                save_data(st.session_state.df)
                st.session_state.ocr = None
                st.success("✅ Saved!")
                st.rerun()

# ──────────── ENTRY TAB ────────────
with tab_entry:
    st.subheader("Manual Entry")
    c1, c2 = st.columns(2)
    with c1:
        e_date = st.date_input("Date", value=date.today(), key="ed")
        e_party = st.text_input("Party Name *", key="ep")
        e_amt = st.number_input("Taxable Amount (₹) *", min_value=0.0, format="%.2f", key="ea")
        e_city = st.text_input("City", key="ec")
    with c2:
        e_gstin = st.text_input("GSTIN (15 chars) *", max_chars=15, key="eg", placeholder="23CIAPB7825A1Z0")
        e_inv = st.text_input("Invoice ID *", key="ei")
        e_rate = st.selectbox("GST Rate", [5,12,18],
                              format_func=lambda x: f"{x}% ({'Fabric' if x==5 else 'Readymade' if x==12 else 'Synthetic'})", key="er")
        e_state = st.selectbox("State", [""]+STATES, key="es")

    c3, c4 = st.columns(2)
    with c3:
        e_tr = st.number_input("Transport/Fare (₹)", min_value=0.0, format="%.2f", key="etr")
    with c4:
        e_pin = st.text_input("PIN Code", max_chars=6, key="epin")

    if e_gstin and len(e_gstin)==15:
        info = decode_gstin(e_gstin.upper())
        if info: st.info(f"🔍 **{info['state']}** | {info['entityType']} | PAN: `{info['pan']}`")

    # Saved GSTINs
    if len(st.session_state.df)>0:
        uq = st.session_state.df[["gstin","party_name"]].drop_duplicates()
        if len(uq)>0:
            opts = ["-- Saved GSTINs --"]+[f"{r['gstin']} - {r['party_name']}" for _,r in uq.iterrows()]
            st.selectbox("Quick fill", opts, key="epick")

    if e_amt > 0:
        tax = calc_tax(e_amt, e_rate, e_state, e_tr)
        st.caption(f"**{'Inter-State → IGST' if tax['inter'] else 'Intra-State → CGST + SGST'}**")
        mc = st.columns(4)
        if tax["inter"]:
            mc[0].metric("IGST", f"₹{tax['igst']}", f"@{tax['igst_r']}%")
        else:
            mc[0].metric("CGST", f"₹{tax['cgst']}", f"@{tax['cgst_r']}%")
            mc[1].metric("SGST", f"₹{tax['sgst']}", f"@{tax['sgst_r']}%")
        mc[2].metric("Transport", f"₹{tax['transport']}")
        mc[3].metric("Grand Total", f"₹{tax['grand']}")

    if st.button("💾 Save Entry", use_container_width=True, type="primary", key="esave"):
        if not e_party or not e_gstin or e_amt<=0 or not e_inv:
            st.warning("Fill all * fields")
        elif len(e_gstin)!=15:
            st.warning("GSTIN must be 15 characters")
        else:
            tax = calc_tax(e_amt, e_rate, e_state, e_tr)
            row = pd.DataFrame([{
                "date":e_date,"party_name":e_party,"gstin":e_gstin.upper(),
                "invoice_id":e_inv,"taxable_amount":e_amt,
                "gst_rate":e_rate,"cgst_r":tax["cgst_r"],"cgst":tax["cgst"],
                "sgst_r":tax["sgst_r"],"sgst":tax["sgst"],"igst_r":tax["igst_r"],
                "igst":tax["igst"],"transport":tax["transport"],"grand_total":tax["grand"],
                "city":e_city,"state":e_state,"pin_code":e_pin,
                "quarter":get_quarter(e_date),"fy":get_fy(e_date),"inter_state":tax["inter"]
            }])
            st.session_state.df = pd.concat([st.session_state.df, row], ignore_index=True)
            save_data(st.session_state.df)
            st.success("✅ Entry saved!")
            st.rerun()

# ──────────── LEDGER TAB ────────────
with tab_ledger:
    st.subheader("Ledger")
    df = st.session_state.df

    fc1, fc2, fc3 = st.columns([2,2,1])
    with fc1:
        sel_fy = st.selectbox("Financial Year", FY_LIST, key="lfy")
    with fc2:
        qopts = ["ALL"]+[q["label"] for q in QUARTERS]
        sel_q = st.selectbox("Quarter", qopts, key="lq")

    if len(df)>0:
        filt = df[df["fy"]==sel_fy].copy()
        if sel_q != "ALL":
            qid = sel_q.split(" ")[0]
            filt = filt[filt["quarter"]==qid]

        if len(filt)>0:
            t_amt = filt["taxable_amount"].sum()
            t_cgst = filt["cgst"].sum()
            t_sgst = filt["sgst"].sum()
            t_igst = filt["igst"].sum()
            t_tr = filt["transport"].sum()
            t_grand = filt["grand_total"].sum()

            r1 = st.columns(3)
            r1[0].metric("Taxable", f"₹{t_amt:,.0f}")
            r1[1].metric("CGST", f"₹{t_cgst:,.0f}")
            r1[2].metric("SGST", f"₹{t_sgst:,.0f}")
            r2 = st.columns(3)
            r2[0].metric("IGST", f"₹{t_igst:,.0f}")
            r2[1].metric("Transport", f"₹{t_tr:,.0f}")
            r2[2].metric("Grand Total", f"₹{t_grand:,.0f}", delta=f"{len(filt)} entries")

            st.markdown("---")

            # CSV download
            csv = filt.to_csv(index=False)
            st.download_button("📥 Download CSV", csv, f"Bunser_{sel_fy}_{sel_q}.csv", "text/csv",
                               use_container_width=True)

            st.markdown("---")

            for _, row in filt.sort_values("date", ascending=False).iterrows():
                il = "Inter" if row.get("inter_state") else "Intra"
                td = ""
                if row.get("cgst",0)>0: td += f"CGST:₹{row['cgst']:.0f} SGST:₹{row['sgst']:.0f} "
                if row.get("igst",0)>0: td += f"IGST:₹{row['igst']:.0f} "
                if row.get("transport",0)>0: td += f"Fare:₹{row['transport']:.0f}"

                st.markdown(f"""<div class="entry-card">
                    <div style="display:flex;justify-content:space-between;">
                        <div>
                            <div style="font-weight:600;font-size:14px;color:#eee;">{row['party_name']}</div>
                            <div style="font-size:11px;color:#555;font-family:monospace;">{row['gstin']}</div>
                        </div>
                        <div style="text-align:right;">
                            <div style="font-weight:700;font-size:16px;color:#e879f9;">₹{row['grand_total']:,.2f}</div>
                            <div style="font-size:10px;color:#555;">{row['date']} | {row.get('quarter','')} | {il}</div>
                        </div>
                    </div>
                    <div style="font-size:11px;color:#777;margin-top:4px;">
                        Base: ₹{row['taxable_amount']:,.0f} | {td} | Inv: {row['invoice_id']}
                    </div>
                    <div style="font-size:10px;color:#444;margin-top:2px;">
                        {row.get('city','')}{', '+row['state'] if row.get('state') else ''}
                    </div>
                </div>""", unsafe_allow_html=True)
        else:
            st.info(f"No entries for {sel_fy} {sel_q}")
    else:
        st.info("No entries yet.")

    if len(df)>0:
        with st.expander("🗑️ Delete Entry"):
            delidx = st.number_input("Row index", min_value=0, max_value=max(len(df)-1,0), step=1, key="delidx")
            if delidx < len(df):
                r = df.iloc[delidx]
                st.caption(f"{r.get('party_name','')} | {r.get('invoice_id','')} | ₹{r.get('grand_total','')}")
            if st.button("Delete", key="delbtn"):
                st.session_state.df = df.drop(index=delidx).reset_index(drop=True)
                save_data(st.session_state.df)
                st.rerun()

# ──────────── GST TAB ────────────
with tab_gst:
    st.subheader("Textile GST Rates")
    st.info("**Rule:** ≤ ₹1,000/piece → **5%** | > ₹1,000 → **12%** (from 1 Jan 2022)")

    for r in GST_INFO:
        c1,c2 = st.columns([5,1])
        c1.markdown(f"**{r['desc']}**"); c1.caption(f"HSN: `{r['hsn']}`")
        color = "🟢" if r["rate"]==5 else "🟡"
        c2.markdown(f"### {color} {r['rate']}%")
        st.divider()

    st.markdown("**Intra-State (within MP):** CGST (rate/2) + SGST (rate/2)")
    st.markdown("**Inter-State (outside MP):** IGST (full rate)")
    st.markdown("---")
    st.markdown("**Filing Quarters:** Q1: Apr-Jun | Q2: Jul-Sep | Q3: Oct-Dec | Q4: Jan-Mar")

# ──────────── ALERTS TAB ────────────
with tab_alerts:
    st.subheader("GST Filing Alerts")
    for a in alerts:
        bg = "#1a0808" if a["urgent"] else "#141420"
        bc = "#5c2020" if a["urgent"] else "#2a2a3a"
        col = "#f87171" if a["urgent"] else "#4ade80"
        nc = "#f87171" if a["urgent"] else "#eee"
        st.markdown(f"""<div style="background:{bg};border:1px solid {bc};border-radius:8px;padding:10px 14px;margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;">
                <div>
                    <div style="font-weight:700;color:{nc};">{a['form']}</div>
                    <div style="font-size:12px;color:#888;">{a['desc']} | {'Monthly' if a['monthly'] else 'Annual'}</div>
                </div>
                <div style="text-align:right;">
                    <div style="font-weight:700;font-size:16px;color:{col};">{a['days']}d</div>
                    <div style="font-size:11px;color:#888;">{a['date_str']}</div>
                </div>
            </div>
        </div>""", unsafe_allow_html=True)

    st.markdown("---")
    st.warning("""**Penalties:**
- GSTR-1/3B late: ₹50/day (NIL: ₹20/day), max ₹5,000
- 18% interest on outstanding tax
- GSTR-9 late: ₹200/day (₹100 CGST + ₹100 SGST)""")

st.markdown("---")
st.caption("Bunser Textiles GST Ledger | Huntergger Main Shop")
