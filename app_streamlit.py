import streamlit as st
import pandas as pd
import json, os, re, requests, base64
from datetime import datetime, date
from io import BytesIO

# ── Config ──
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gst_ledger.csv")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
FY_LIST = ["2026-27","2025-26","2024-25","2023-24","2022-23"]
COLS = ["date","party_name","gstin","invoice_id","taxable_amount","gst_rate",
        "cgst_r","cgst","sgst_r","sgst","igst_r","igst","transport","grand_total",
        "city","state","pin_code","quarter","fy","inter_state"]
STATES = ["Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat",
    "Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh",
    "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab","Rajasthan",
    "Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand","West Bengal",
    "Delhi","Jammu & Kashmir","Ladakh","Chandigarh","Puducherry"]
STATE_CODES = {"01":"Jammu & Kashmir","02":"Himachal Pradesh","03":"Punjab","04":"Chandigarh",
    "05":"Uttarakhand","06":"Haryana","07":"Delhi","08":"Rajasthan","09":"Uttar Pradesh",
    "10":"Bihar","11":"Sikkim","12":"Arunachal Pradesh","13":"Nagaland","14":"Manipur",
    "15":"Mizoram","16":"Tripura","17":"Meghalaya","18":"Assam","19":"West Bengal",
    "20":"Jharkhand","21":"Odisha","22":"Chhattisgarh","23":"Madhya Pradesh",
    "24":"Gujarat","27":"Maharashtra","29":"Karnataka","30":"Goa","32":"Kerala",
    "33":"Tamil Nadu","34":"Puducherry","36":"Telangana","37":"Andhra Pradesh"}
DEADLINES = [
    {"form":"GSTR-1","desc":"Outward supplies","day":11,"monthly":True},
    {"form":"GSTR-3B","desc":"Summary return","day":20,"monthly":True},
    {"form":"GSTR-9","desc":"Annual return","month":12,"day":31,"monthly":False}]
GST_INFO = [
    {"hsn":"5208-5212","desc":"Woven cotton fabrics","rate":5},
    {"hsn":"551311","desc":"Teery/Rubiya blend","rate":5},
    {"hsn":"52","desc":"Raw cotton","rate":5},
    {"hsn":"6101-6117","desc":"Knitted apparel","rate":12},
    {"hsn":"6201-6217","desc":"Woven apparel","rate":12},
    {"hsn":"6301-6310","desc":"Blankets, bed linen","rate":12}]

# ── Helpers ──
def get_key():
    try: return st.secrets["gemini"]["api_key"]
    except: return ""

def decode_gstin(g):
    if not g or len(g)!=15: return None
    sc=g[:2]; s=STATE_CODES.get(sc,""); p=g[5]
    t={"P":"Proprietorship","C":"Company","F":"Firm/LLP","H":"HUF","T":"Trust"}
    return {"state":s,"code":sc,"type":t.get(p,"Unknown"),"pan":g[2:12]}

def get_qtr(d):
    if isinstance(d,str):
        try: d=datetime.strptime(d,"%Y-%m-%d").date()
        except: return "Q1"
    m=d.month
    if 4<=m<=6: return "Q1"
    if 7<=m<=9: return "Q2"
    if 10<=m<=12: return "Q3"
    return "Q4"

def get_fy(d):
    if isinstance(d,str):
        try: d=datetime.strptime(d,"%Y-%m-%d").date()
        except: return "2025-26"
    m,y=d.month,d.year
    return f"{y}-{str(y+1)[2:]}" if m>=4 else f"{y-1}-{str(y)[2:]}"

def calc(amt,rate,pstate,transport):
    a=float(amt or 0); r=float(rate or 5); f=float(transport or 0)
    home=st.session_state.get("home_state","Madhya Pradesh")
    inter=bool(pstate and pstate!=home)
    cr=0 if inter else r/2; sr=0 if inter else r/2; ir=r if inter else 0
    c=round(a*cr/100,2); s=round(a*sr/100,2); i=round(a*ir/100,2)
    return {"inter":inter,"cr":cr,"sr":sr,"ir":ir,"cgst":c,"sgst":s,"igst":i,"fare":f,"grand":round(a+c+s+i+f,2)}

def alerts():
    now=datetime.now(); out=[]
    for d in DEADLINES:
        if d["monthly"]:
            dl=datetime(now.year,now.month,d["day"])
            if dl<now:
                nm=now.month+1 if now.month<12 else 1; ny=now.year if now.month<12 else now.year+1
                dl=datetime(ny,nm,d["day"])
        else:
            dl=datetime(now.year,d["month"],d["day"])
            if dl<now: dl=datetime(now.year+1,d["month"],d["day"])
        days=(dl-now).days+1
        out.append({**d,"days":days,"ds":dl.strftime("%d/%m/%Y"),"urg":days<=7})
    return out

def load():
    if os.path.exists(DATA_FILE):
        try:
            df=pd.read_csv(DATA_FILE)
            if "date" in df.columns: df["date"]=pd.to_datetime(df["date"],errors="coerce").dt.date
            for c in COLS:
                if c not in df.columns: df[c]="" if c in ["party_name","gstin","invoice_id","city","state","pin_code","quarter","fy"] else 0
            # Deduplicate by invoice_id
            df = df.drop_duplicates(subset=["invoice_id"], keep="last")
            return df
        except: pass
    return pd.DataFrame(columns=COLS)

def save(df):
    df.drop_duplicates(subset=["invoice_id"], keep="last").to_csv(DATA_FILE, index=False)

def add(entry):
    df=st.session_state.df
    inv=entry.get("invoice_id","")
    if inv and inv in df["invoice_id"].values:
        st.warning(f"⚠️ Invoice **{inv}** already exists! Duplicate skipped.")
        return False
    st.session_state.df=pd.concat([df,pd.DataFrame([entry])],ignore_index=True)
    save(st.session_state.df)
    return True

def gemini_extract(fbytes,mime,key):
    prompt="""Extract from this Indian GST invoice. Return ONLY valid JSON, no backticks:
{"partyName":"seller name","gstin":"15-digit GSTIN","amount":"taxable amount number","invoiceId":"invoice number","date":"YYYY-MM-DD","city":"city","state":"Indian state","pinCode":"PIN","gstRate":"5 or 12 or 18","transport":"freight number or 0","igst":"IGST amount or 0","cgst":"CGST or 0","sgst":"SGST or 0","grandTotal":"net amount number"}
Use "" for missing, 0 for missing numbers."""
    b64=base64.b64encode(fbytes).decode()
    r=requests.post(f"{GEMINI_URL}?key={key}",headers={"Content-Type":"application/json"},
        json={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":mime,"data":b64}}]}]},timeout=60)
    if r.status_code!=200: raise Exception(f"API {r.status_code}: {r.text[:200]}")
    txt=r.json()["candidates"][0]["content"]["parts"][0]["text"]
    txt=re.sub(r"```json\s*","",txt); txt=re.sub(r"```\s*","",txt)
    return json.loads(txt.strip())

# ── App ──
st.set_page_config(page_title="Textile GST Ledger", page_icon="🧵", layout="centered")
st.markdown("""<style>
.block-container{max-width:720px;padding-top:1rem;}
.ec{background:#141418;border:1px solid #23232e;border-radius:10px;padding:12px 14px;margin-bottom:6px;}
div[data-testid="stMetric"]{background:#141418;border:1px solid #23232e;border-radius:8px;padding:8px;}
.stTabs [data-baseweb="tab-list"]{gap:0;}
.stTabs [data-baseweb="tab"]{padding:8px 16px;}
</style>""",unsafe_allow_html=True)

if "df" not in st.session_state: st.session_state.df=load()
if "ocr" not in st.session_state: st.session_state.ocr=None

# ── Sidebar ──
with st.sidebar:
    st.markdown("### Settings")
    sn=st.text_input("Shop Name",value=st.session_state.get("sn",""),placeholder="Your shop name")
    hs=st.selectbox("Home State (for tax calc)",STATES,
        index=STATES.index(st.session_state.get("home_state","Madhya Pradesh")) if st.session_state.get("home_state","Madhya Pradesh") in STATES else 12)
    if sn: st.session_state.sn=sn
    st.session_state.home_state=hs
    st.divider()
    st.caption(f"📁 {len(st.session_state.df)} entries saved")
    st.caption(f"File: `{DATA_FILE}`")

# ── Header ──
name=st.session_state.get("sn","Textile GST Ledger")
st.markdown(f"## {name}")

al=alerts()
urg=[a for a in al if a["urg"]]
if urg:
    st.error(" · ".join([f"**{a['form']}** {a['days']}d left" for a in urg]))

t1,t2,t3,t4=st.tabs(["📷 Scan","📝 Entry","📒 Ledger","📊 Data"])

# ── SCAN ──
with t1:
    key=get_key()
    if not key: key=st.text_input("Gemini API Key",type="password",help="https://aistudio.google.com/apikey")

    mode=st.radio("Input:",["📁 File/Gallery","📸 Camera"],horizontal=True,label_visibility="collapsed")
    fbytes=None; mime="image/jpeg"

    if mode=="📁 File/Gallery":
        up=st.file_uploader("Upload invoice",type=["pdf","png","jpg","jpeg","webp"],label_visibility="collapsed")
        if up:
            fbytes=up.read(); mime=up.type or "image/jpeg"
            if "image" in mime: st.image(fbytes,use_container_width=True)
            else: st.caption(f"📄 {up.name} · {len(fbytes)//1024}KB")
    else:
        cam=st.camera_input("Capture invoice",label_visibility="collapsed")
        if cam: fbytes=cam.read(); mime="image/jpeg"

    if fbytes and key:
        if st.button("🤖 Extract",use_container_width=True,type="primary"):
            with st.spinner("Reading..."):
                try:
                    st.session_state.ocr=gemini_extract(fbytes,mime,key)
                    st.success("✅ Done")
                except Exception as e: st.error(f"❌ {e}"); st.session_state.ocr=None

    if st.session_state.ocr:
        r=st.session_state.ocr
        st.divider()
        c1,c2=st.columns(2)
        with c1:
            try: od=datetime.strptime(r.get("date",""),"%Y-%m-%d").date()
            except: od=date.today()
            sd=st.date_input("Date",value=od,key="sd")
            sp=st.text_input("Party *",value=r.get("partyName",""),key="sp")
            sa=st.text_input("Taxable Amt *",value=str(r.get("amount","")),key="sa")
        with c2:
            sg=st.text_input("GSTIN *",value=r.get("gstin",""),key="sg",max_chars=15)
            si=st.text_input("Invoice *",value=str(r.get("invoiceId","")),key="si")
            gr=int(r.get("gstRate",5)) if str(r.get("gstRate","5")).isdigit() else 5
            sr=st.selectbox("GST%",[5,12,18],index=[5,12,18].index(gr) if gr in [5,12,18] else 0,key="sr")

        cc1,cc2,cc3=st.columns(3)
        with cc1:
            rs=r.get("state","")
            ss=st.selectbox("State",[""]+STATES,index=(STATES.index(rs)+1) if rs in STATES else 0,key="ss")
        with cc2: st2=st.number_input("Transport",value=float(r.get("transport",0) or 0),min_value=0.0,key="st2")
        with cc3: spin=st.text_input("PIN",value=str(r.get("pinCode","")),max_chars=6,key="spin")
        sc2=st.text_input("City",value=r.get("city",""),key="sc2")

        if sg and len(sg)==15:
            gi=decode_gstin(sg.upper())
            if gi: st.caption(f"🔍 {gi['state']} · {gi['type']} · PAN: {gi['pan']}")

        if sa:
            try:
                tx=calc(sa,sr,ss,st2)
                mc=st.columns(4)
                if tx["inter"]: mc[0].metric("IGST",f"₹{tx['igst']}",f"@{tx['ir']}%")
                else: mc[0].metric("CGST",f"₹{tx['cgst']}",f"@{tx['cr']}%"); mc[1].metric("SGST",f"₹{tx['sgst']}",f"@{tx['sr']}%")
                mc[2].metric("Fare",f"₹{tx['fare']}"); mc[3].metric("Total",f"₹{tx['grand']}")
            except: pass

        if st.button("✅ Save",use_container_width=True,type="primary",key="ssave"):
            if not sp or not sg or not sa or not si: st.warning("Fill all * fields")
            elif len(sg)!=15: st.warning("GSTIN = 15 chars")
            else:
                tx=calc(sa,sr,ss,st2)
                ok=add({"date":sd,"party_name":sp,"gstin":sg.upper(),"invoice_id":si,
                    "taxable_amount":float(sa),"gst_rate":sr,"cgst_r":tx["cr"],"cgst":tx["cgst"],
                    "sgst_r":tx["sr"],"sgst":tx["sgst"],"igst_r":tx["ir"],"igst":tx["igst"],
                    "transport":tx["fare"],"grand_total":tx["grand"],"city":sc2,"state":ss,
                    "pin_code":spin,"quarter":get_qtr(sd),"fy":get_fy(sd),"inter_state":tx["inter"]})
                if ok: st.session_state.ocr=None; st.success("✅ Saved!"); st.rerun()

# ── ENTRY ──
with t2:
    c1,c2=st.columns(2)
    with c1:
        ed=st.date_input("Date",value=date.today(),key="ed")
        ep=st.text_input("Party Name *",key="ep")
        ea=st.number_input("Taxable Amount ₹ *",min_value=0.0,format="%.2f",key="ea")
    with c2:
        eg=st.text_input("GSTIN *",max_chars=15,key="eg",placeholder="15 digit GSTIN")
        ei=st.text_input("Invoice ID *",key="ei")
        er=st.selectbox("GST Rate",[5,12,18],format_func=lambda x:f"{x}%",key="er")

    ec1,ec2,ec3=st.columns(3)
    with ec1: es=st.selectbox("State",[""]+STATES,key="es")
    with ec2: etr=st.number_input("Transport ₹",min_value=0.0,format="%.2f",key="etr")
    with ec3: epin=st.text_input("PIN",max_chars=6,key="epin")
    ecity=st.text_input("City",key="ecity")

    if eg and len(eg)==15:
        gi=decode_gstin(eg.upper())
        if gi: st.caption(f"🔍 {gi['state']} · {gi['type']} · PAN: {gi['pan']}")

    # Saved GSTINs
    if len(st.session_state.df)>0:
        uq=st.session_state.df[["gstin","party_name"]].drop_duplicates()
        if len(uq)>0:
            st.selectbox("Saved GSTINs",["--"]+[f"{r['gstin']} - {r['party_name']}" for _,r in uq.iterrows()],key="epick")

    if ea>0:
        tx=calc(ea,er,es,etr)
        mc=st.columns(4)
        if tx["inter"]: mc[0].metric("IGST",f"₹{tx['igst']}",f"@{tx['ir']}%")
        else: mc[0].metric("CGST",f"₹{tx['cgst']}",f"@{tx['cr']}%"); mc[1].metric("SGST",f"₹{tx['sgst']}",f"@{tx['sr']}%")
        mc[2].metric("Fare",f"₹{tx['fare']}"); mc[3].metric("Total",f"₹{tx['grand']}")

    if st.button("💾 Save",use_container_width=True,type="primary",key="esave"):
        if not ep or not eg or ea<=0 or not ei: st.warning("Fill all * fields")
        elif len(eg)!=15: st.warning("GSTIN = 15 chars")
        else:
            tx=calc(ea,er,es,etr)
            ok=add({"date":ed,"party_name":ep,"gstin":eg.upper(),"invoice_id":ei,
                "taxable_amount":ea,"gst_rate":er,"cgst_r":tx["cr"],"cgst":tx["cgst"],
                "sgst_r":tx["sr"],"sgst":tx["sgst"],"igst_r":tx["ir"],"igst":tx["igst"],
                "transport":tx["fare"],"grand_total":tx["grand"],"city":ecity,"state":es,
                "pin_code":epin,"quarter":get_qtr(ed),"fy":get_fy(ed),"inter_state":tx["inter"]})
            if ok: st.success("✅ Saved!"); st.rerun()

# ── LEDGER ──
with t3:
    df=st.session_state.df
    c1,c2,c3=st.columns(3)
    with c1: sfy=st.selectbox("FY",FY_LIST,key="lfy")
    with c2: sq=st.selectbox("Qtr",["ALL","Q1 Apr-Jun","Q2 Jul-Sep","Q3 Oct-Dec","Q4 Jan-Mar"],key="lq")
    with c3: so=st.selectbox("Sort",["Newest","Oldest","Amt ↓","Amt ↑","Name"],key="lso")

    if len(df)>0:
        f=df[df["fy"]==sfy].copy()
        if sq!="ALL": f=f[f["quarter"]==sq.split(" ")[0]]
        # Sort
        if so=="Newest": f=f.sort_values("date",ascending=False)
        elif so=="Oldest": f=f.sort_values("date",ascending=True)
        elif so=="Amt ↓": f=f.sort_values("grand_total",ascending=False,key=lambda x:x.astype(float))
        elif so=="Amt ↑": f=f.sort_values("grand_total",ascending=True,key=lambda x:x.astype(float))
        else: f=f.sort_values("party_name")

        if len(f)>0:
            ta=f["taxable_amount"].astype(float).sum()
            tc=f["cgst"].astype(float).sum()
            ts=f["sgst"].astype(float).sum()
            ti=f["igst"].astype(float).sum()
            tt=f["transport"].astype(float).sum()
            tg=f["grand_total"].astype(float).sum()

            m1,m2,m3=st.columns(3)
            m1.metric("Taxable",f"₹{ta:,.0f}"); m2.metric("CGST+SGST",f"₹{tc+ts:,.0f}"); m3.metric("IGST",f"₹{ti:,.0f}")
            m4,m5,m6=st.columns(3)
            m4.metric("Transport",f"₹{tt:,.0f}"); m5.metric("Grand Total",f"₹{tg:,.0f}"); m6.metric("Entries",f"{len(f)}")
            st.divider()

            for n,(_,row) in enumerate(f.iterrows(),1):
                il="Inter" if row.get("inter_state") else "Intra"
                td=""
                if float(row.get("cgst",0) or 0)>0: td+=f"C:₹{float(row['cgst']):.0f} S:₹{float(row['sgst']):.0f} "
                if float(row.get("igst",0) or 0)>0: td+=f"I:₹{float(row['igst']):.0f} "
                if float(row.get("transport",0) or 0)>0: td+=f"F:₹{float(row['transport']):.0f}"
                st.markdown(f"""<div class="ec">
                <div style="display:flex;justify-content:space-between;">
                <div><span style="color:#7c3aed;font-size:10px;font-weight:700;">#{n}</span>
                <span style="font-weight:600;color:#eee;margin-left:4px;">{row['party_name']}</span>
                <div style="font-size:10px;color:#555;font-family:monospace;">{row['gstin']} · Inv: {row['invoice_id']}</div></div>
                <div style="text-align:right;"><div style="font-weight:700;font-size:15px;color:#e879f9;">₹{float(row['grand_total']):,.2f}</div>
                <div style="font-size:10px;color:#555;">{row['date']} · {row.get('quarter','')} · {il}</div></div>
                </div>
                <div style="font-size:10px;color:#666;margin-top:3px;">Base ₹{float(row['taxable_amount']):,.0f} | {td}</div>
                </div>""",unsafe_allow_html=True)
        else: st.info(f"No entries for {sfy} {sq}")
    else: st.info("No entries yet")

# ── DATA ──
with t4:
    df=st.session_state.df
    if len(df)>0:
        dc1,dc2=st.columns(2)
        with dc1: dfy=st.selectbox("FY",["ALL"]+FY_LIST,key="dfy")
        with dc2: dq=st.selectbox("Qtr",["ALL","Q1","Q2","Q3","Q4"],key="dq")

        v=df.copy()
        if dfy!="ALL": v=v[v["fy"]==dfy]
        if dq!="ALL": v=v[v["quarter"]==dq]
        v=v.sort_values("date",ascending=False)

        st.caption(f"{len(v)} entries")
        st.dataframe(v,use_container_width=True,hide_index=True,
            column_config={
                "date":st.column_config.DateColumn("Date",format="DD/MM/YYYY"),
                "party_name":"Party","gstin":"GSTIN","invoice_id":"Invoice",
                "taxable_amount":st.column_config.NumberColumn("Taxable",format="₹%.2f"),
                "gst_rate":st.column_config.NumberColumn("Rate",format="%d%%"),
                "cgst":st.column_config.NumberColumn("CGST",format="₹%.2f"),
                "sgst":st.column_config.NumberColumn("SGST",format="₹%.2f"),
                "igst":st.column_config.NumberColumn("IGST",format="₹%.2f"),
                "transport":st.column_config.NumberColumn("Transport",format="₹%.2f"),
                "grand_total":st.column_config.NumberColumn("Grand Total",format="₹%.2f"),
                "quarter":"Qtr","fy":"FY",
                "inter_state":st.column_config.CheckboxColumn("Inter?"),
            },
            column_order=["date","party_name","gstin","invoice_id","taxable_amount",
                          "gst_rate","cgst","sgst","igst","transport","grand_total","quarter","fy","inter_state"]
        )

        st.divider()
        # Summary
        sc=["taxable_amount","cgst","sgst","igst","transport","grand_total"]
        sm=v[sc].astype(float).sum()
        st.markdown(f"**Totals:** Taxable ₹{sm['taxable_amount']:,.0f} · CGST ₹{sm['cgst']:,.0f} · SGST ₹{sm['sgst']:,.0f} · IGST ₹{sm['igst']:,.0f} · Transport ₹{sm['transport']:,.0f} · **Grand ₹{sm['grand_total']:,.0f}**")

        st.divider()
        d1,d2,d3,d4=st.columns(4)
        fname=f"Ledger_{dfy}_{dq}"
        with d1: st.download_button("CSV",v.to_csv(index=False),f"{fname}.csv","text/csv",use_container_width=True)
        with d2:
            try:
                buf=BytesIO()
                with pd.ExcelWriter(buf,engine="openpyxl") as w: v.to_excel(w,index=False,sheet_name="Ledger")
                st.download_button("Excel",buf.getvalue(),f"{fname}.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)
            except: st.button("Excel",disabled=True,use_container_width=True,help="pip install openpyxl")
        with d3: st.download_button("JSON",v.to_json(orient="records",indent=2,date_format="iso"),f"{fname}.json","application/json",use_container_width=True)
        with d4: st.download_button("HTML",v.to_html(index=False),f"{fname}.html","text/html",use_container_width=True)

        st.divider()
        with st.expander("🗑️ Delete"):
            di=st.number_input("Row #",min_value=0,max_value=max(len(df)-1,0),step=1,key="di")
            if di<len(df):
                r=df.iloc[di]; st.caption(f"{r.get('party_name','')} · Inv: {r.get('invoice_id','')} · ₹{r.get('grand_total','')}")
            if st.button("Delete",key="dbtn"):
                st.session_state.df=df.drop(index=di).reset_index(drop=True)
                save(st.session_state.df); st.rerun()
        if st.button("🔄 Reload from file",key="rel"): st.session_state.df=load(); st.rerun()
    else: st.info("No data yet")

# ── Footer: GST info + Alerts in sidebar ──
with st.sidebar:
    st.divider()
    st.markdown("### GST Rates")
    for r in GST_INFO:
        st.markdown(f"**{r['rate']}%** · {r['desc']} `{r['hsn']}`")
    st.divider()
    st.markdown("### Filing Deadlines")
    for a in alerts():
        c="🔴" if a["urg"] else "🟢"
        st.markdown(f"{c} **{a['form']}** · {a['days']}d · {a['ds']}")
    st.caption(f"GSTR-1: 11th | GSTR-3B: 20th | Late: ₹50/day")
