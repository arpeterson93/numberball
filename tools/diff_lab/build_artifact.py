"""Step 3 (after build_model.py, build_matchup.py and build_own_state.py): embed swings, contexts and run-value tables into template.html.

    python tools/diff_lab/build_artifact.py [output.html]

Default output is tools/diff_lab/cache/diff_distribution.html (publish it with the Artifact tool).
"""
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)
import json, warnings, logging, pickle, collections
warnings.filterwarnings('ignore'); logging.disable(logging.CRITICAL)
import pandas as pd, numpy as np, utils
R=pd.read_pickle(CACHE/"state_rows.pkl"); M=pickle.load(open(CACHE/"state_meta.pkl",'rb'))
TR,RE,cell,GC=M['TR'],M['RE'],M['cell'],M['GC']
LOGOBC={0:'000',1:'001',2:'010',3:'100',4:'011',5:'101',6:'110',7:'111'}
INN=6
def wp_after(inning,half,outs_a,brc_a,lead_a):
    remh=utils.remaining_half_innings(inning,half,INN)
    if half=='bottom' and remh==1 and lead_a>0: return 1.0
    if outs_a>=3:
        rn=remh-1
        if rn>=1:
            o=utils.get_win_probability_interpolated(rn,0,'000',-lead_a)
            return None if o is None else 1.0-o
        return 1.0 if lead_a>0 else (0.5 if lead_a==0 else 0.0)
    return utils.get_win_probability_interpolated(remh,outs_a,LOGOBC[brc_a],lead_a)
def trans(outs,brc,cls):
    name=GC[cls]; t=TR.get((outs,brc,name))
    if t: return t[0],t[1],t[2],'t'
    c=cell.get((outs*8+brc,name))
    if c:
        (runs,eo,br),_=c.most_common(1)[0]; return runs,min(3,outs+eo),br,'m'
    return None
src=collections.Counter()
for s in range(24):
    for c in range(len(GC)):
        if (s,GC[c]) in cell:
            t=trans(s//8,s%8,c); src[t[3] if t else 'none']+=1
print('cell sources',dict(src))
# RV table
RVT=[[None]*len(GC) for _ in range(24)]
for s in range(24):
    o,b=s//8,s%8
    for c in range(len(GC)):
        t=trans(o,b,c)
        if not t: continue
        runs,oa,br,_=t
        re_a=RE[(oa,br)] if oa<3 else 0.0
        RVT[s][c]=round(re_a+runs-RE[(o,b)],3)
# contexts
ctxk=R[['inn','half','outs','brc','lead']].drop_duplicates().itertuples(index=False,name=None)
ctx={};ctxl=[]
for k in ctxk:
    inn,half,outs,brc,lead=k
    h='bottom' if half else 'top'
    wb=utils.get_win_probability_interpolated(utils.remaining_half_innings(inn,h,INN),outs,LOGOBC[brc],lead)
    w=[]
    for c in range(len(GC)):
        t=trans(outs,brc,c)
        if not t or wb is None: w.append(None); continue
        runs,oa,br,_=t
        wa=wp_after(inn,h,oa,br,lead+runs)
        w.append(None if wa is None else round(wa-wb,3))
    ctx[k]=len(ctxl); ctxl.append([inn,half,outs,brc,lead,w])
print('ctx',len(ctxl))
# hit-and-run evidence: with a runner on 1st/2nd and < 2 outs, a 1BWH at a diff where the normal table says plain 1B
# (or 1BWH2) can only come from a hit and run (it turns every 1B into 1BWH); same for 2BWH vs 2B. See tools/hnr_audit.py.
aud=pd.read_pickle(CACHE/"hnr_audit.pkl")
aud=aud[((aud.obc.str[2]=="1")|(aud.obc.str[1]=="1"))&(aud.outs<2)]
f1=aud[(aud.result=="1BWH")&aud.n.isin(["1B","1BWH2"])&(aud.h=="1BWH")].id
f2=aud[(aud.result=="2BWH")&(aud.n=="2B")&(aud.h=="2BWH")].id
HF={**{int(i):1 for i in f1},**{int(i):2 for i in f2}}
R["hf"]=R.pid.map(HF).fillna(0).astype(int)
print("hit-and-run evidence swings:",R.hf.value_counts().to_dict())
elig=R[((R.brc.isin([1,2,4,5])))&(R.outs<2)]
print(elig.groupby("season").hf.agg(["size",lambda x:(x>0).sum()]).T.to_string())
# string tables
def idx(col):
    u=sorted(R[col].fillna('?').unique()); m={v:i for i,v in enumerate(u)}; return u,R[col].fillna('?').map(m)
U=sorted(set(R.team.fillna('?'))|set(R.dteam.fillna('?'))); TM={v:i for i,v in enumerate(U)}
T=U; t=R.team.fillna('?').map(TM); dt=R.dteam.fillna('?').map(TM)
B,b=idx('bat'); P,p=idx('pit'); RW,rw=idx('raw')
SIMPLE={'HR':0,'3B':1,'2B':2,'2BWH':2,'1B':3,'1BWH':3,'1BWH2':3,'IF1B':3,'BB':4,'K':5,'SacF':7,'DSacF':7,'FO':7,'DFO':7,'PO':7}
for c in ['GORA','FC','FC3rd','FCH','GO','DP','DP21','DP31','DPH1','DPRun','LODP','TP']: SIMPLE[c]=6
rows=[]
for i,r in enumerate(R.itertuples(index=False)):
    rows.append([r.season,int(t.iloc[i]),int(b.iloc[i]),int(p.iloc[i]),r.d,SIMPLE[r.raw],int(rw.iloc[i]),r.g,r.cls,
                 ctx[(r.inn,r.half,r.outs,r.brc,r.lead)],round(r.rv,2),round(r.wpa,3),r.hf,int(dt.iloc[i])])
# evidence stats per season: swings with a runner on 1st/2nd, under 2 outs (a call is possible) vs at 2 outs (it is not)
def pat(df): return ((df.result=="1BWH")&df.n.isin(["1B","1BWH2"])&(df.h=="1BWH"))|((df.result=="2BWH")&(df.n=="2B")&(df.h=="2BWH"))
A=pd.read_pickle(CACHE/"hnr_audit.pkl"); A=A[A.season>=5]; A=A[(A.obc.str[2]=="1")|(A.obc.str[1]=="1")]
HS=[]
for se,g in A.groupby("season"):
    lg=g[g.outs<2]; ct=g[g.outs==2]
    HS.append([int(se),int(pat(lg).sum()),len(lg),int(pat(ct).sum()),len(ct)])
print(HS)
# calculator model (tools/diff_lab/build_model.py): result mix at each diff stacked from every swing's ranges
import base64
MD=pickle.load(open(CACHE/"model.pkl","rb"))
def b64(a): return base64.b64encode(np.round(np.clip(a,0,1)*65535).astype('<u2').tobytes()).decode()
HSTATES=[o*8+b for o in (0,1) for b in (1,2,4,5)]
Ee=MD["PN"][(R.outs*8+R.brc).values,:,R.d.values]; Aa=np.eye(len(GC))[R.cls.values]
tv=[[int(se),round(float(np.abs(Ee[g.index.values].sum(0)-Aa[g.index.values].sum(0)).sum()/2/len(g)),4)] for se,g in R.groupby("season")]
dd=Aa.sum(0)-Ee.sum(0)
mdiff=[[GC[i],int(round(Aa.sum(0)[i])),int(round(Ee.sum(0)[i]))] for i in np.argsort(-np.abs(dd))[:6]]
model=dict(pn=b64(MD["PN"]),ph=b64(MD["PH"][HSTATES]),hs=HSTATES,p8=b64(MD["P8"]),tv=tv,diffs=mdiff)
# each player's own matchup distribution (build_matchup.py): one byte per share, 8 classes x 51 ten-wide buckets
MU=pickle.load(open(CACHE/"matchup.pkl","rb"))
own={side:MU[side] for side in ("bat","pit")}      # "season|name" -> curve index
own["cv"]={side:[base64.b64encode(c.tobytes()).decode() for c in MU["curves"][side]] for side in ("bat","pit")}
# state-aware own expectation per swing (build_own_state.py): probabilities of each result under the player's own build,
# turned into expected runs / WPA with the same tables as the league mix; int16 (runs x1000, WPA x10000), -32768 = none
OS=pickle.load(open(CACHE/"own_state.pkl","rb"))
RV0=np.array([[np.nan if v is None else v for v in row] for row in RVT]); VALID=~np.isnan(RV0); RV0=np.nan_to_num(RV0)
WX=np.array([[0.0 if v is None else v for v in c[5]] for c in ctxl])
S8=(R.outs*8+R.brc).values; CXI=np.array([r[9] for r in rows])
def own_ev(P,idx):
    s8=S8[idx]; Pv=P*VALID[s8]; tot=Pv.sum(1); ok=tot>0; pr=Pv/np.where(ok,tot,1)[:,None]
    xr=(pr*RV0[s8]).sum(1); xw=(pr*WX[CXI[idx]]).sum(1)
    q=lambda v,k:np.where(ok,np.clip(np.round(v*k),-32767,32767),-32768).astype('<i2')
    return q(xr,1000),q(xw,10000)
allr=np.arange(len(R)); bx,bw=own_ev(OS["PB"],allr); px,pw=own_ev(OS["PP"],allr)
nohave=~OS["have"]
for a_ in (bx,bw,px,pw): a_[nohave]=-32768
hi=sorted(OS["HB"]); hv={}
if hi:
    hidx=np.array(hi); hbx,hbw=own_ev(np.array([OS["HB"][i] for i in hi]),hidx); hpx,hpw=own_ev(np.array([OS["HP"][i] for i in hi]),hidx)
    hv={int(i):[int(hbx[j]),int(hbw[j]),int(hpx[j]),int(hpw[j])] for j,i in enumerate(hi)}
own["st"]=dict(bx=base64.b64encode(bx.tobytes()).decode(),bw=base64.b64encode(bw.tobytes()).decode(),
               px=base64.b64encode(px.tobytes()).decode(),pw=base64.b64encode(pw.tobytes()).decode(),h=hv)
data=dict(own=own,model=model,hnr=HS,teams=T,bat=B,pit=P,raw=RW,rows=rows,GC=GC,ctx=ctxl,rvt=RVT)
html=open(HERE/'template.html',encoding='utf-8').read().replace('/*DATA*/null',json.dumps(data,separators=(',',':')))
open(sys.argv[1] if len(sys.argv)>1 else CACHE/'diff_distribution.html','w',encoding='utf-8').write(html)
print(len(rows),len(html)/1e6)
