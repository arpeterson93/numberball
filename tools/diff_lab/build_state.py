"""Step 2: per-swing run value (RE24) and WPA from the play log -> cache/state_rows.pkl, state_meta.pkl.

Run-value convention: the score columns are post-play in S1-10 and pre-play in S11+, detected per
season from solo HRs. import_BRC.csv eOuts is outs AFTER the play, not outs added.
"""
import os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
CACHE = HERE / "cache"
CACHE.mkdir(exist_ok=True)
import json, warnings, logging
warnings.filterwarnings('ignore'); logging.disable(logging.CRITICAL)
import pandas as pd, numpy as np, collections
import utils
df=pd.read_pickle(CACHE/"plays.pkl")
df=df[(df.game_type=='live')&df.game_id.notna()].sort_values(['game_id','play_num']).reset_index(drop=True)
GC=['HR','3B','2B','2BWH','1B','1BWH','1BWH2','IF1B','BB','SacF','DSacF','FO','DFO','PO','GORA','FC','FC3rd','FCH','GO','K','DP','DP21','DP31','DPH1']
MERGE={'DPRun':'DP','TP':'DP','LODP':'DP'}
SIMPLE={'HR':0,'3B':1,'2B':2,'2BWH':2,'1B':3,'1BWH':3,'1BWH2':3,'IF1B':3,'BB':4,'K':5,'SacF':7,'DSacF':7,'FO':7,'DFO':7,'PO':7}
for c in ['GORA','FC','FC3rd','FCH','GO','DP','DP21','DP31','DPH1']: SIMPLE[c]=6
OBC2BRC={'000':0,'001':1,'010':2,'100':3,'011':4,'101':5,'110':6,'111':7}
# RE matrix
rem=pd.read_csv('run_expectancy_matrix.csv')
LAB={0:'Empty',1:'1B',2:'2B',3:'3B',4:'1&2B',5:'1&3B',6:'2&3B',7:'BL'}
RE={}
for _,r in rem.iterrows():
    RE[(int(r.outs),[k for k,v in LAB.items() if v==r.obc][0])]=float(r.expected_runs)
assert len(RE)==24, len(RE)
# transitions
b=pd.read_csv('import_BRC.csv',dtype=str)
k=b.Situation.str.split('_',n=2,expand=True)
TR={}
for sit,runs,eo,ob in zip(b.Situation,b.Runs,b.eOuts,b.OBC):
    try: r_,e_,o_=int(runs),int(eo),int(ob)
    except Exception: continue
    o,br,res=sit.split('_',2)
    TR[(int(o),int(br),res)]=(r_,e_,o_)
print(len(TR))
# convention per season
df['tot']=df.away_score+df.home_score
g=df.groupby('game_id')
df['prev_tot']=g.tot.shift(1); df['next_tot']=g.tot.shift(-1)
hr=df[(df.result=='HR')&(df.obc=='000')&(df.play_type=='Swing')]
conv={}
for s,x in hr.groupby('season'):
    post=((x.tot-x.prev_tot)==1).mean(); pre=((x.next_tot-x.tot)==1).mean()
    conv[int(s)]='post' if post>pre else 'pre'
print(conv)
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
    obc=utils.BRC_TO_OBC_LOG[brc_a]
    return utils.get_win_probability_interpolated(remh,outs_a,obc,lead_a)
LOGOBC={0:'000',1:'001',2:'010',3:'100',4:'011',5:'101',6:'110',7:'111'}
utils.BRC_TO_OBC_LOG=LOGOBC
def wp_before(inning,half,outs,brc,lead):
    remh=utils.remaining_half_innings(inning,half,INN)
    return utils.get_win_probability_interpolated(remh,outs,LOGOBC[brc],lead)
# walk games
rows=[];cell_trans=collections.defaultdict(collections.Counter)
A=df.to_dict('records')
idx=0;n=len(A)
game_start=0
games=df.groupby('game_id').indices
for gid,ix in games.items():
    pl=[A[i] for i in ix]
    for j,p in enumerate(pl):
        if p['play_type']!='Swing' or p['season']<5 or pd.isna(p.get('diff')): continue
        res=p['result']; res=MERGE.get(res,res)
        if res not in GC: continue
        prev=pl[j-1] if j>0 else None; nxt=pl[j+1] if j+1<len(pl) else None
        outs=int(p['outs']); brc=OBC2BRC.get(p['obc']); 
        if brc is None or outs>2: continue
        c=conv[int(p['season'])]
        if c=='post':
            ba=(prev['away_score'] if prev else 0); bh=(prev['home_score'] if prev else 0)
            runs=(p['away_score']+p['home_score'])-(ba+bh)
        else:
            ba=p['away_score']; bh=p['home_score']
            runs=(nxt['away_score']+nxt['home_score']-(ba+bh)) if nxt else None
        half=p['half']; inn=int(p['inning'])
        lead_b=(bh-ba) if half=='bottom' else (ba-bh)
        same=nxt is not None and nxt['inning']==inn and nxt['half']==half
        if runs is None or runs<0:
            tr=TR.get((outs,brc,res)); runs=tr[0] if tr else 0
        if same:
            o_a=int(nxt['outs']); br_a=OBC2BRC.get(nxt['obc'],0)
            re_a=RE[(o_a,br_a)] if o_a<3 else 0.0
        else:
            o_a=3; br_a=0; re_a=0.0
        rv=re_a+runs-RE[(outs,brc)]
        lead_a=lead_b+runs
        if nxt is None and not same:
            ended_game = inn>=INN
        else: ended_game=False
        if ended_game:
            wa=1.0 if lead_a>0 else (0.5 if lead_a==0 else 0.0)
        else:
            wa=wp_after(inn,half,o_a,br_a,lead_a)
        wb=wp_before(inn,half,outs,brc,lead_b)
        wpa=None if (wa is None or wb is None) else wa-wb
        cell_trans[(outs*8+brc,res)][(runs,o_a-outs if o_a<3 else 3-outs,br_a)]+=1
        rows.append(dict(season=int(p['season']),team=p['off_team'],bat=p['batter_name'],pit=p['pitcher_name'],d=int(p['diff']),raw=p['result'],
                         cls=GC.index(res),g=int(gid),pid=int(p['id']),dteam=p['def_team'],outs=outs,brc=brc,inn=inn,half=0 if half=='top' else 1,lead=int(lead_b),rv=rv,wpa=wpa))
R=pd.DataFrame(rows)
print(len(R), R.rv.mean(), R.rv.sum(), R.wpa.isna().mean(), R.wpa.mean())
print(R.groupby('outs').rv.mean().round(3).to_dict())
print(R.groupby('cls').rv.mean().round(2).rename(lambda i:GC[i]).to_dict())
R.to_pickle(CACHE/"state_rows.pkl")
import pickle
pickle.dump(dict(TR=TR,RE=RE,cell=dict(cell_trans),GC=GC),open(CACHE/"state_meta.pkl",'wb'))
