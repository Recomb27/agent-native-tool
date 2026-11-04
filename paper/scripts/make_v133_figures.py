#!/usr/bin/env python3
"""V13.3 main figures: original/final tools and the original no-tool reference.
Native SVG typography and palette preserve the accepted manuscript style.
"""
from pathlib import Path
import argparse, csv, json, math, html, subprocess, hashlib, shutil
from datetime import datetime, timezone
import pymupdf as fitz
import numpy as np
from fontTools.ttLib import TTFont
ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
OUT=ROOT.parent/'outputs'/'figures'
W=468.0
FONT="Latin Modern Roman"
INK="#23313A"; MID="#68757E"; GRID="#CFD8DE"; PALE="#F5F8FA"
BLUE="#B6CEE6"; APRICOT="#F1CFB6"; GREEN="#96C2BD"
BL="#52758F"; AL="#A76B3F"
ORDER=["motif-search","sequence-properties","pairwise-alignment","codon-usage","transcription-translation","reverse-complement","sequence-slicing","sequence-statistics","filter-sequences","msa-statistics","distance-calculations","restriction-sites"]
SHORT=["Motif search","Seq. properties","Pairwise alignment","Codon usage","Translation","Reverse complement","Sequence slicing","Seq. statistics","Sequence filtering","MSA statistics","Sequence distances","Restriction sites"]
FONT_FILES={b:ROOT/'assets'/'fonts'/('lmroman10-bold.otf' if b else 'lmroman10-regular.otf') for b in [False,True]}
FONTS={b:TTFont(p) for b,p in FONT_FILES.items()}
def read_csv(path):
    with Path(path).open(newline="",encoding="utf-8-sig") as f:return list(csv.DictReader(f))
def load_scores():
    rows=read_csv(DATA/'six_dimension_scores.csv')
    d={(r['arm'],r['tool'],r['dimension']):int(r['score']) for r in rows}
    assert len(rows)==len(d)==144
    return {a:{t:[d[a,t,f'D{i}'] for i in range(1,7)] for t in ORDER} for a in ['original','adapted']}


def text_width(value, size=10, bold=False):
    font = FONTS[bold]
    cmap = font.getBestCmap()
    return sum(font["hmtx"].metrics[cmap.get(ord(c), ".notdef")][0] for c in str(value)) / font["head"].unitsPerEm * size

class SVGBase:
    def __init__(self, height, title):
        self.height = float(height)
        self.labels = []
        self.parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="468pt" height="{height}pt" viewBox="0 0 468 {height}">',
            f'<title>{html.escape(title)}</title>',
            '<defs><marker id="arrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L6,3 L0,6 Z" fill="' + INK + '"/></marker></defs>',
            f'<rect width="468" height="{height}" fill="white"/>',
        ]

    def text(self, x, y, value, size=10, bold=False, anchor="start", color=INK, limit=None):
        value = str(value)
        if size == 11 and y == 12:
            y = 13  # Preserve a positive text-metric top margin in the exported PDF.
        tw = text_width(value, size, bold)
        if limit is not None:
            assert tw <= limit + .2, (value, tw, limit)
        left = x - (tw if anchor == "end" else tw / 2 if anchor == "middle" else 0)
        assert left >= -.2 and left + tw <= W + .2, (value, left, tw)
        assert size >= 10, (value, size)
        self.parts.append(f'<text x="{x:.3f}" y="{y:.3f}" font-family="{FONT}" font-size="{size}" font-weight="{"bold" if bold else "normal"}" text-anchor="{anchor}" fill="{color}">{html.escape(value)}</text>')
        self.labels.append({"text": value, "x": x, "baseline_y": y, "font_pt": size, "bold": bold, "width_pt": tw})

    def line(self, x1, y1, x2, y2, color=GRID, width=.6, dash="", arrow=False):
        self.parts.append(f'<path d="M{x1:.3f},{y1:.3f} L{x2:.3f},{y2:.3f}" fill="none" stroke="{color}" stroke-width="{width}"' + (f' stroke-dasharray="{dash}"' if dash else "") + (' marker-end="url(#arrow)"' if arrow else "") + '/>')

    def rect(self, x, y, width, height, fill, stroke="none", sw=.6, radius=0):
        self.parts.append(f'<rect x="{x:.3f}" y="{y:.3f}" width="{width:.3f}" height="{height:.3f}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def circle(self, x, y, r, fill, stroke=INK, sw=.8):
        self.parts.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>')

    def polygon(self, points, fill="none", stroke=GRID, sw=.6, opacity=1, dash=""):
        pts = " ".join(f"{x:.4f},{y:.4f}" for x, y in points)
        self.parts.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" stroke-width="{sw}"' + (f' stroke-dasharray="{dash}"' if dash else "") + '/>')

    def end(self):
        return "\n".join(self.parts + ["</svg>"]) + "\n"

class SVG(SVGBase):
    def path(self,d,stroke=INK,sw=.8,fill='none',opacity=1,dash='',arrow=False):
        self.parts.append(f'<path d="{d}" stroke="{stroke}" stroke-width="{sw}" fill="{fill}" opacity="{opacity}"'+(f' stroke-dasharray="{dash}"' if dash else '')+(' marker-end="url(#arrow)"' if arrow else '')+'/>')
    def polyline(self,xy,stroke=INK,sw=1,fill='none',opacity=1,dash=''):
        self.path('M'+' L'.join(f'{x:.3f},{y:.3f}' for x,y in xy),stroke,sw,fill,opacity,dash)
    def title(self,x,y,title):self.text(x,y,title,10,True)
    def panel(self,x,y,w,h,label,title):
        self.rect(x,y,w,h,'white',GRID,.75,3)
        self.rect(x+1,y+1,w-2,22,PALE,radius=2)
        self.text(x+7,y+15,label+'  '+title,10,True,limit=w-14)

def legend(s,x,y):
    s.line(x,y,x+18,y,BL,1.4);s.circle(x+9,y,2,BLUE,BL,.65);s.text(x+24,y+3,'Original')
    s.line(x+85,y,x+103,y,AL,1.4,'4 2');s.rect(x+92,y-2,4,4,APRICOT,AL,.65);s.text(x+109,y+3,'Adapted')

def radar(s,cx,cy,r,values,labels=True,fill_opacity=.19,marker=1.6,label_size=10,d4_inset=0):
    angles=[-math.pi/2+i*math.pi/3 for i in range(6)]
    def pts(v):return [(cx+r*v[i]/10*math.cos(a),cy+r*v[i]/10*math.sin(a)) for i,a in enumerate(angles)]
    for v in [5,10]:s.polygon(pts([v]*6),stroke=GRID,sw=.65)
    for a in angles:s.line(cx,cy,cx+r*math.cos(a),cy+r*math.sin(a),GRID,.5)
    for arm,fill,line,dash in [('original',BLUE,BL,''),('adapted',APRICOT,AL,'3 2')]:
        p=pts(values[arm]);s.polygon(p,fill,line,1.15,fill_opacity,dash)
        for x,y in p:
            if arm=='original':s.circle(x,y,marker,fill,line,.55)
            else:s.rect(x-marker,y-marker,marker*2,marker*2,fill,line,.55)
    if labels:
        for i,a in enumerate(angles):
            distance=r+12-(d4_inset if i==3 else 0)
            s.text(cx+distance*math.cos(a),cy+distance*math.sin(a)+3,f'D{i+1}',label_size,anchor='middle')

def tool_tile(s,x,y,w=17,h=12,kind=0):
    s.rect(x,y,w,h,'white',MID,.65,1)
    if kind%3==0:
        for j in range(3):s.line(x+3,y+3+j*3,x+w-3,y+3+j*3,BL if j%2==0 else AL,.8)
    elif kind%3==1:
        for j in range(3):s.circle(x+4+4*j,y+6,1.4,BLUE,BL,.5)
    else:
        s.path(f'M{x+3},{y+8} L{x+6},{y+4} L{x+10},{y+7} L{x+14},{y+3}',BL,1)

def person(s,cx,cy):
    s.circle(cx,cy-7,4,'white',INK,1)
    s.path(f'M{cx-9},{cy+10} Q{cx-8},{cy} {cx},{cy} Q{cx+8},{cy} {cx+9},{cy+10}',INK,1.1,'white')

def agent_icon(s,cx,cy):
    s.rect(cx-10,cy-9,20,17,'white',INK,1,4)
    s.circle(cx-4,cy-2,1.6,GREEN,INK,.65);s.circle(cx+4,cy-2,1.6,GREEN,INK,.65)
    s.line(cx-3,cy+3,cx+3,cy+3,INK,.8);s.line(cx,cy-9,cx,cy-13,INK,.8);s.circle(cx,cy-15,1.7,GREEN,INK,.6)

def build_overview(scores):
    s=SVG(282,'A unified six-dimensional diagnosis, adaptation and evaluation of bioinformatics tools')
    s.panel(0,0,228,134,'A','From researcher to research agent')
    s.panel(240,0,228,134,'B','Six dimensions, one evaluation')
    s.panel(0,145,228,137,'C','Protein engineering as the testbed')
    s.panel(240,145,228,137,'D','Diagnose, adapt, and evaluate')
    s.text(56,37,'Researcher',10,True,'middle');s.text(173,37,'LLM agent',10,True,'middle')
    # Both views contain a comparable six-tool set. Edges encode responsibility,
    # not measured throughput or exact independent software projects.
    nodes=[(-35,-23),(15,-23),(-42,0),(25,0),(-35,24),(15,24)]
    for center in [56,173]:
        for i,(dx,dy) in enumerate(nodes):
            x,y=center+dx,76+dy
            s.line(center,77,x+8.5,y+6,GRID if center==56 else BL,.7)
            tool_tile(s,x,y,kind=i)
        if center==56:person(s,center,77)
        else:agent_icon(s,center,77)
    s.line(107,77,124,77,INK,1.2,arrow=True)
    s.text(56,126,'Human interprets',10,anchor='middle')
    s.text(173,126,'Agent links evidence',10,anchor='middle')
    dims=[('D1','Information','meaning and limits'),('D2','Intervention','inspect and act on parts'),('D3','Experience','recover prior evidence'),('D4','Evolvability','check successor tools'),('D5','Valid use','justify the next action'),('D6','Contribution','outcomes and resources')]
    for i,(d,name,meaning) in enumerate(dims):
        y=37+i*17
        s.circle(254,y-3,6.2,BLUE if i%2==0 else GREEN,'none',0)
        s.text(254,y,d,10,True,'middle')
        s.text(265,y,name,10,True)
        # At this size a short second column preserves readable full concepts
        # while captions state the exact long-form dimension names.
        s.text(340,y,meaning,10,limit=122)
    # Data strip and protein-engineering decisions.
    s.text(79,181,'Historical TadA data',10,True,'middle')
    for row in range(5):
        for col in range(8):
            s.rect(13+col*4.4,192+row*4.5,3.3,2.6,[BLUE,APRICOT,GREEN][(row+col)%3])
    s.path('M65,208 C69,188 79,189 82,203 C85,218 94,217 99,199 C105,184 114,196 111,208',BL,2.4)
    s.line(51,204,61,204,INK,.85,arrow=True)
    s.text(72,224,'Over 1M DNA sequences',10,anchor='middle')
    s.rect(124,188,100,29,PALE,GRID,.6,2)
    s.text(174,200,'Simulated research',10,True,anchor='middle')
    s.text(174,213,'campaign',10,True,anchor='middle')
    s.line(114,204,122,204,INK,.85,arrow=True)
    # Three computational action stages with return edge. These are replay actions.
    for x,w,t in [(10,59,'Prepare'),(83,59,'Screen'),(156,61,'Confirm')]:
        s.rect(x,230,w,21,PALE,GRID,.65,3);s.text(x+w/2,244,t,10,True,'middle')
    s.line(70,240,81,240,INK,.8,arrow=True);s.line(143,240,154,240,INK,.8,arrow=True)
    s.path('M187,252 L187,261 L39,261 L39,253',INK,.75,arrow=True)
    s.text(114,276,'One of 12 tool entry points',10,anchor='middle')
    # Real aggregate radar, not an idealized "all improved" icon.
    means={a:[float(np.mean([scores[a][t][i] for t in ORDER])) for i in range(6)] for a in ['original','adapted']}
    radar(s,295,217,31,means,marker=1.2)
    s.text(295,277,'Mean profile (0-10)',10,anchor='middle')
    for i,t in enumerate(['Run and diagnose','Targeted repairs','Check and rerun']):
        yy=182+i*24
        s.rect(352,yy,106,19,[BLUE,PALE,APRICOT][i],GRID,.55,2)
        s.text(405,yy+13,t,10,True,'middle')
        if i<2:s.line(405,yy+20,405,yy+26,INK,.7,arrow=True)
    s.line(355,260,369,260,BL,1.2);s.text(374,263,'Original')
    s.line(355,274,369,274,AL,1.2,'3 2');s.text(374,277,'Adapted')
    return s


def build_radars(scores):
    s=SVG(312,'Twelve tools under one six-dimensional post-run rubric')
    legend(s,150,10)
    for idx,t in enumerate(ORDER):
        cx=58.5+117*(idx%4);cy=75+91*(idx//4)
        s.text(cx,cy-42,SHORT[idx],10,True,'middle',limit=113)
        radar(s,cx,cy,22.5,{a:scores[a][t] for a in scores},marker=1.3,d4_inset=6)
    s.text(234,307,'0–10 ordinal scale; D1–D4 improve in all twelve tools.',10,anchor='middle')
    return s


def load_campaigns():
    pairs=json.loads((DATA/'paired_endpoints.json').read_text())
    endpoint={(r['tool'],int(r['repeat'])):r for r in pairs}
    assert len(pairs)==len(endpoint)==120
    points=read_csv(DATA/'accepted_state_points.csv')
    states={}
    for r in points:
        k=(r['arm'],r['tool'],int(r['repeat']))
        states.setdefault(k,[]).append(r)
    assert len(states)==240
    for (a,t,r),seq in states.items():
        assert all(float(x['day'])<=float(y['day']) and int(x['action_index'])<=int(y['action_index'])
                   for x,y in zip(seq,seq[1:]))
        assert float(seq[0]['day'])==0
        assert float(seq[0]['cash'])==100000 and float(seq[0]['activity'])==1
        assert all(float(x['activity'])<=float(y['activity'])+1e-12 for x,y in zip(seq,seq[1:]))
        for sm,em in [('cash','cash'),('activity','activity'),('day','days')]:
            assert abs(float(seq[-1][sm])-float(endpoint[t,r][a][em]))<1e-7
    curves=read_csv(DATA/'daily_values.csv')
    assert len(curves)==240*181
    daily={k:{'cash':np.full(181,np.nan),'activity':np.full(181,np.nan)} for k in states}
    for row in curves:
        k=(row['arm'],row['tool'],int(row['repeat']));day=int(row['day'])
        for m in ['cash','activity']:
            assert math.isnan(daily[k][m][day])
            daily[k][m][day]=float(row[m])
    for k,seq in states.items():
        days=np.array([float(r['day']) for r in seq])
        idx=np.searchsorted(days,np.arange(181),side='right')-1
        assert idx.min()>=0
        for m in ['cash','activity']:
            reconstructed=np.array([float(r[m]) for r in seq])[idx]
            assert np.allclose(daily[k][m],reconstructed,rtol=0,atol=1e-7)
    draws=np.random.default_rng(20261104).integers(0,10,size=(10000,10))
    summary={}
    for a in ['original','adapted']:
        summary[a]={}
        for m in ['cash','activity']:
            clusters=np.array([[daily[a,t,r][m] for t in ORDER] for r in range(1,11)]).mean(axis=1)
            low,high=np.percentile(clusters[draws].mean(axis=1),[2.5,97.5],axis=0)
            summary[a][m]={'mean':clusters.mean(axis=0),'low':low,'high':high}
    checks={'accepted_state_points':len(points),'daily_values':len(curves),'campaigns_per_arm':120,
            'daily_reconstruction':'last accepted state, including terminal carry; all 43,440 rows verified',
            'endpoint_checks':240,'repeat_clusters':10,'bootstrap_draws':10000,'bootstrap_seed':20261104,
            'pointwise_intervals':True,
            'terminal_means':{a:{m:float(summary[a][m]['mean'][-1]) for m in ['cash','activity']} for a in summary}}
    return endpoint,states,daily,summary,checks


def step_points(days,values,x,y,w,h,lo,hi):
    out=[]
    for day,v in zip(days,values):
        assert lo-1e-8<=v<=hi+1e-8,(day,v,lo,hi)
        xx=x+float(day)/180*w;yy=y+h-(float(v)-lo)/(hi-lo)*h
        if out:out.append((xx,out[-1][1]))
        out.append((xx,yy))
    return out


def plot_axes(s,x,y,w,h,lo,hi,ticks,title,xticks=True):
    s.text(x,y-13,title,10,True)
    for v in ticks:
        yy=y+h-(v-lo)/(hi-lo)*h
        s.line(x,yy,x+w,yy,GRID,.5)
        s.text(x-5,yy+3,f'{v:g}'.replace('-','−'),10,anchor='end')
    s.line(x,y,x,y+h,MID,.65);s.line(x,y+h,x+w,y+h,MID,.65)
    for d in [0,60,120,180]:
        xx=x+d/180*w
        s.line(xx,y+h,xx,y+h+3,MID,.65)
        if xticks:s.text(xx,y+h+15,d,10,anchor='middle')


def load_original_no_tool(endpoint, summary):
    """Add ten physical original controls, never twelve duplicated logical copies."""
    rows = read_csv(DATA/'original_no_tool_daily.csv')
    assert len(rows) == 1810
    by_repeat = {r: sorted([x for x in rows if int(x['repeat']) == r],
                          key=lambda x: int(x['day'])) for r in range(1,11)}
    for rep, seq in by_repeat.items():
        assert len(seq) == 181 and [int(x['day']) for x in seq] == list(range(181))
        assert len({x['assignment_key'] for x in seq}) == 1
        assert all(x['cohort'] == 'original_no_tool' for x in seq)
        e = endpoint[ORDER[0], rep]['original_control']
        assert all(endpoint[t,rep]['original_control']==e for t in ORDER)
        for m in ['cash', 'activity', 'reward', 'cost']:
            assert abs(float(seq[-1][m])-float(e[m])) < 1e-7
        assert abs(float(seq[-1]['terminal_day'])-float(e['days'])) < 1e-7
        assert float(seq[0]['cash']) == 100000 and float(seq[0]['activity']) == 1
    draws = np.random.default_rng(20261104).integers(0,10,size=(10000,10))
    summary['original_no_tool'] = {}
    for metric in ['cash', 'activity']:
        clusters = np.array([[float(x[metric]) for x in by_repeat[r]] for r in range(1,11)])
        lower, upper = np.percentile(clusters[draws].mean(axis=1),[2.5,97.5],axis=0)
        summary['original_no_tool'][metric] = {
            'mean': clusters.mean(axis=0), 'low': lower, 'high': upper}
    return {'physical_original_controls':10,'original_no_tool_rows':1810,
            'terminal_matches':10,'control_repeated_per_tool':False,
            'intervals':'Pointwise percentile 95%; 10000 identical whole-repeat draws for all cohorts',
            'uncertainty_display':'Continuous pointwise 95% step ribbons for all three cohorts'}


def build_daily(summary):
    s=SVG(214,'Original and adapted tool trajectories with the original no-tool reference')
    specs=[(23,'Original (120)',BL,''),(159,'Adapted (120)',AL,'4 2'),
           (295,'Original no-tool (10)',MID,'8 3')]
    for x,label,line,dash in specs:
        fill={BL:BLUE,AL:APRICOT,MID:'#C7CDD1'}[line]
        s.polygon([(x,6),(x+20,6),(x+20,14),(x,14)],fill,'none',0,.30)
        s.line(x,10,x+20,10,line,1.5,dash)
        s.text(x+26,13,label,10)
    specs=[('cash',1000,0,300,[0,100,200,300],39,197,'A  Cash (thousand units)'),
           ('activity',1,0,8,[0,2,4,6,8],283,174,'B  Confirmed activity')]
    for metric,div,lo,hi,ticks,x,w,title in specs:
        y,h=50,126
        plot_axes(s,x,y,w,h,lo,hi,ticks,title)
        # Draw the broad reference ribbon first, with the same opacity and
        # pointwise interval definition as the two tool arms. Means remain on top.
        for arm,fill in [('original_no_tool','#C7CDD1'),('original',BLUE),('adapted',APRICOT)]:
            d=summary[arm][metric]
            upper=step_points(range(181),d['high']/div,x,y,w,h,lo,hi)
            lower=step_points(range(181),d['low']/div,x,y,w,h,lo,hi)
            s.polygon(upper+list(reversed(lower)),fill,'none',0,.30)
        for arm,line,dash in [('original_no_tool',MID,'8 3'),('original',BL,''),('adapted',AL,'4 2')]:
            d=summary[arm][metric]
            s.polyline(step_points(range(181),d['mean']/div,x,y,w,h,lo,hi),line,1.5,dash=dash)
        s.text(x+w/2,211,'Simulated day',10,anchor='middle')
    return s


def build_case(endpoint,states):
    case=json.loads((DATA/'case_sequence_properties_r08.json').read_text())
    tool,rep=case['tool'],int(case['repeat'])
    assert (tool,rep)==('sequence-properties',8)
    seqs={arm:states[arm,tool,rep] for arm in ['original','adapted']}
    calls={arm:[c for c in case['calls'] if c['arm']==arm] for arm in seqs}
    assert [c['day'] for c in calls['adapted']]==[0,15,15,35,84,84,166,166]
    assert len(calls['original'])==1 and calls['original'][0]['day']==0
    for arm in seqs:
        for metric in ['cash','activity','cost','days','reward','profit']:
            assert abs(case['endpoints'][arm][metric]-endpoint[tool,rep][arm][metric]) < 1e-7
    s=SVG(334,'A positive sequence-properties pair connects refreshed evidence to subsequent actions')
    s.text(4,13,'Sequence properties / repeat 08',11,True)
    legend(s,291,10)
    boxes=[(0,152,'A  Day 15: sequence checks',[
        'Review flags new backgrounds.',
        'Backgrounds 2/3: same DNA;',
        '501 nt; ACGT; no internal stop',
        'Access: 30/50-aa windows.',
        'Next: prepare backgrounds',
        '3 and 2 (days 18 and 21).']),
        (158,152,'B  Day 84: return check',[
        'Observed activity: 9.01.',
        'Noise bounds: 7.51–11.27.',
        'Incremental return: 383–516k.',
        'Next: confirm the candidate;',
        'activity 8.63 on day 96.']),
        (316,152,'C  Day 166: return check',[
        'Observed activity: 10.03.',
        'Noise bounds: 8.36–12.53.',
        'Lower return bound: negative.',
        '14 days: confirm or stop.',
        'Next: confirm; activity 10.14',
        'on day 178.'])]
    for bx,bw,title,lines in boxes:
        s.rect(bx,25,bw,96,PALE,GRID,.65,3)
        s.text(bx+6,39,title,10,True,limit=bw-12)
        for i,line in enumerate(lines):
            s.text(bx+6,53+12*i,line,10,limit=bw-12)
    x,y,w,h,lo,hi=48,144,406,96,-100,400
    xx=lambda day:x+day/180*w
    yy=lambda profit:y+h-(profit-lo)/(hi-lo)*h
    plot_axes(s,x,y,w,h,lo,hi,[-100,0,100,200,300,400],'')
    s.text(4,131,'Net return, k',10,True)
    s.line(x,yy(0),x+w,yy(0),MID,.75,'3 2')
    # Leaders identify the evidence available at a decision point. They are not
    # arrows asserting that the tool caused the later reward.
    for call,bx,label in [(calls['adapted'][2],76,'A'),(calls['adapted'][5],234,'B'),(calls['adapted'][7],392,'C')]:
        px,py=xx(call['day']),yy((call['cash']-100000)/1000)
        mid_y=131
        if label=='C':
            s.path(f'M461,121 L461,{py} L{px+6},{py}',MID,.65,dash='2 2')
        else:
            s.path(f'M{bx},121 L{bx},{mid_y} L{px},{mid_y} L{px},{py}',MID,.65,dash='2 2')
    for arm,line,fill,dash in [('original',BL,BLUE,''),('adapted',AL,APRICOT,'4 2')]:
        seq=seqs[arm]
        points=step_points([float(q['day']) for q in seq],[(float(q['cash'])-100000)/1000 for q in seq],x,y,w,h,lo,hi)
        s.polyline(points,line,1.5,dash=dash)
        for a,b in zip(seq,seq[1:]):
            if float(b['activity'])>float(a['activity'])+1e-12:
                px,py=xx(float(b['day'])),yy((float(b['cash'])-100000)/1000)
                s.rect(px-2.7,py-2.7,5.4,5.4,fill,line,.8)
        ex,ey=points[-1];s.line(ex,ey-4,ex,ey+4,line,1.5)
    for call,label in [(calls['adapted'][2],'A'),(calls['adapted'][5],'B'),(calls['adapted'][7],'C')]:
        px,py=xx(call['day']),yy((call['cash']-100000)/1000)
        s.circle(px,py,5.9,'white',AL,.9);s.text(px,py+3.3,label,10,True,'middle',AL)
    # Point annotations do not create a second y axis. Activity is the confirmed
    # endpoint associated with each indicated state, not an interpolated series.
    first=next(q for q in seqs['adapted'] if int(q['action_index'])==17)
    s.text(xx(96)+7,yy((float(first['cash'])-100000)/1000)-8,'Confirmed 8.63',10,color=AL)
    last=seqs['adapted'][-1]
    s.text(xx(178)-5,yy((float(last['cash'])-100000)/1000)-10,'10.14; +333.9k',10,anchor='end',color=AL)
    s.text(xx(78)-5,216,'Stop: activity 1; −72.4k',10,anchor='end',color=BL)
    s.line(xx(78),220,xx(78),yy(-72.405)-4,BL,.7)
    s.text(90,166,'Original: sequence audit',10,color=BL)
    s.text(90,178,'then generic checks;',10,color=BL)
    s.text(90,190,'7,180 screened; activity 1.',10,color=BL)
    s.text(248,272,'Simulated day',10,anchor='middle')
    # Two compact call lanes share the simulation-day axis. Stacked marks at a
    # single day preserve separate review and scientific calls without jitter.
    lane_left=x; lane_width=w
    lane_x=lambda day:lane_left+day/180*lane_width
    s.text(39,292,'Original',10,anchor='end',color=BL)
    s.text(39,311,'Adapted',10,anchor='end',color=AL)
    s.line(lane_left,289,lane_left+lane_width,289,GRID,.6)
    s.line(lane_left,308,lane_left+lane_width,308,GRID,.6)
    s.circle(lane_x(0),289,2.7,BLUE,BL,.8);s.text(lane_x(0)+6,292,'1',10,color=BL)
    groups={}
    for c in calls['adapted']:groups.setdefault(c['day'],[]).append(c)
    for day,group in groups.items():
        px=lane_x(day)
        for j,c in enumerate(group):
            py=308+(j-(len(group)-1)/2)*7
            s.rect(px-2.1,py-2.1,4.2,4.2,APRICOT if c['mode']=='visible' else 'white',AL,.8)
        s.text(px+6,311,','.join(str(c['call']) for c in group),10,color=AL)
    s.text(4,331,'Tool calls: filled = scientific computation; open = review.',10,color=MID)
    checks={'tool':tool,'repeat':rep,'selection':case['selection'],
        'all_calls_marked':{'original':1,'adapted':8},
        'expanded_call_days':[15,84,166],'case_stop_days':{a:float(seqs[a][-1]['day']) for a in seqs},
        'net_return':{a:case['endpoints'][a]['profit'] for a in seqs},
        'curve_interpolation':'stepwise accepted states, no terminal carry after stop',
        'noise_bounds_are_confidence_intervals':False,
        'original_also_used_scientific_tool':True}
    return s,checks


def build_quadrants(endpoint):
    means={}
    for tool in ORDER:
        pairs=[endpoint[tool,rep] for rep in range(1,11)]
        means[tool]={m:float(np.mean([q['adapted'][m]-q['original'][m] for q in pairs]))
                     for m in ['activity','profit','cash']}
        assert abs(means[tool]['profit']-means[tool]['cash'])<1e-7
    counts={'both_up':sum(q['activity']>0 and q['profit']>0 for q in means.values()),
            'both_down':sum(q['activity']<0 and q['profit']<0 for q in means.values()),
            'activity_up_return_down':sum(q['activity']>0 and q['profit']<0 for q in means.values()),
            'activity_down_return_up':sum(q['activity']<0 and q['profit']>0 for q in means.values())}
    assert counts=={'both_up':10,'both_down':2,'activity_up_return_down':0,'activity_down_return_up':0}
    assert {t for t,q in means.items() if q['activity']<0}=={'pairwise-alignment','filter-sequences'}
    s=SVG(282,'Per-tool mean changes reveal heterogeneous outcomes across the twelve tools')
    s.text(4,13,'Δ net return (thousand units)',11,True)
    s.text(464,13,'Adapted − Original',10,anchor='end',color=MID)
    x,y,w,h=40,39,417,198
    xlo,xhi,ylo,yhi=-3,5,-100,200
    xx=lambda v:x+(v-xlo)/(xhi-xlo)*w
    yy=lambda v:y+h-(v-ylo)/(yhi-ylo)*h
    for v in [-2,0,2,4]:
        s.line(xx(v),y,xx(v),y+h,MID if v==0 else GRID,.9 if v==0 else .45,'3 2' if v==0 else '')
        s.text(xx(v),y+h+15,str(v).replace('-','−'),10,anchor='middle')
    for v in [-100,0,100,200]:
        s.line(x,yy(v),x+w,yy(v),MID if v==0 else GRID,.9 if v==0 else .45,'3 2' if v==0 else '')
        s.text(x-6,yy(v)+3,str(v).replace('-','−'),10,anchor='end')
    s.line(x,y,x,y+h,MID,.65);s.line(x,y+h,x+w,y+h,MID,.65)
    s.text(x+6,y+13,'Return ↑ / activity ↓: 0',10,color=MID)
    s.text(x+w-5,y+13,'Both higher: 10',10,True,anchor='end',color=MID)
    s.text(x+6,190,'Both lower: 2',10,True,color=MID)
    s.text(x+w-5,y+h-8,'Return ↓ / activity ↑: 0',10,anchor='end',color=MID)
    # Fixed label placements follow the numeric points; leaders prevent overlap
    # in the closely spaced positive cluster without changing the plotted data.
    labels={
      'motif-search':('Motif search',260,149,'start'),
      'sequence-properties':('Seq. properties',389,68,'start'),
      'pairwise-alignment':('Pairwise alignment',110,220,'start'),
      'codon-usage':('Codon usage',332,145,'start'),
      'transcription-translation':('Translation',394,84,'start'),
      'reverse-complement':('Reverse complement',292,96,'end'),
      'sequence-slicing':('Seq. slicing',387,116,'start'),
      'sequence-statistics':('Seq. statistics',286,114,'end'),
      'filter-sequences':('Seq. filtering',129,199,'start'),
      'msa-statistics':('MSA statistics',389,100,'start'),
      'distance-calculations':('Sequence distances',284,130,'end'),
      'restriction-sites':('Restriction sites',255,164,'start')}
    for tool in ORDER:
        q=means[tool];px,py=xx(q['activity']),yy(q['profit']/1000)
        name,lx,ly,anchor=labels[tool]
        tip=lx-3 if anchor=='start' else lx+3
        s.line(px,py,tip,ly-3,MID,.65)
        s.text(lx,ly,name,10,anchor=anchor)
        s.circle(px,py,2.6,APRICOT,AL,.9)
    s.text(249,273,'Δ confirmed activity',11,True,anchor='middle')
    checks={'tools':12,'paired_repeats_per_tool':10,'quadrant_counts':counts,
            'means':means,'net_return_definition':'cash minus 100000 initial units',
            'uncertainty':'Tool-level intervals remain in the full-result tables; no significance encoded by quadrant.'}
    return s,checks


def save(s,name,inputs,checks=None):
    OUT.mkdir(parents=True,exist_ok=True)
    svg=OUT/(name+'.svg');pdf=OUT/(name+'.pdf')
    svg.write_text(s.end())
    if shutil.which('inkscape'):
        subprocess.run(['inkscape',str(svg),'--export-type=pdf','--export-filename='+str(pdf)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    elif name == 'fig4_daily':
        # A portable renderer for this figure's basic SVG geometry. The bundled
        # OTF outlines are converted to TTF for ReportLab, retaining their metrics.
        from font_conversion import register_latin_modern
        from svglib.svglib import svg2rlg
        from reportlab.graphics import renderPDF
        from reportlab import rl_config
        rl_config.invariant = 1
        register_latin_modern(ROOT/'assets'/'fonts', OUT/'font-cache')
        renderPDF.drawToFile(svg2rlg(str(svg)),str(pdf))
        # Remove ReportLab's unused default Helvetica resource. All figure text
        # uses the embedded Latin Modern outline conversion registered above.
        cleaned=pdf.with_suffix('.clean.pdf')
        with fitz.open(pdf) as document:
            document.save(cleaned,garbage=4,clean=True,deflate=True,no_new_id=True)
        cleaned.replace(pdf)
    else:
        raise RuntimeError('Rebuilding Figures 1-3 and 5 requires Inkscape; use --only daily for the V13.3 change.')
    subprocess.run(['pdftoppm','-scale-to','1600','-png','-singlefile',str(pdf),str(OUT/name)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    doc=fitz.open(pdf);assert len(doc)==1
    page=doc[0]
    assert abs(page.rect.width-W)<.01 and abs(page.rect.height-s.height)<.01
    spans=[sp for b in page.get_text('dict')['blocks'] if b['type']==0 for line in b['lines'] for sp in line['spans']]
    assert min(sp['size'] for sp in spans)>=9.99
    bad=[sp for sp in spans if sp['bbox'][0]<-.2 or sp['bbox'][2]>W+.2 or sp['bbox'][1]<-.2 or sp['bbox'][3]>s.height+.2]
    assert not bad,bad
    qa=OUT/'audit';qa.mkdir(exist_ok=True)
    (qa/(name+'.txt')).write_text(page.get_text())
    fonts='\n'.join(str(item) for item in page.get_fonts(full=True))
    assert 'Type 3' not in fonts
    (qa/(name+'_fonts.txt')).write_text(fonts)
    sha=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    def source_label(p):
        try:return str(p.relative_to(ROOT))
        except ValueError:return str(p)
    record={'figure':name,'pdf_sha256':sha(pdf),'svg_sha256':sha(svg),'width_pt':W,'height_pt':s.height,
            'minimum_font_pt':min(sp['size'] for sp in spans),'source_sha256':sha(__file__),
            'data_hashes':{source_label(p):sha(p) for p in inputs},
            'checks':checks or {},'preview_policy':'PNG rendered from exported PDF with Poppler',
            'technical_checks_passed':True,'visual_review':'pending'}
    (qa/(name+'.json')).write_text(json.dumps(record,indent=2)+'\n')
    print(name,pdf.stat().st_size,flush=True)
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='Reproduce V13.3 vector figures from the frozen paper data.')
    parser.add_argument('--paper-root',type=Path,default=ROOT)
    parser.add_argument('--data-dir',type=Path)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--only',choices=['all','profiles','new','daily'],default='daily',
                        help='Regenerate all figures, the score-dependent profiles, or Figures 3–5 only.')
    args=parser.parse_args()
    ROOT=args.paper_root.resolve();DATA=(args.data_dir or ROOT/'data').resolve()
    OUT=(args.output_dir or ROOT.parent/'outputs'/'figures').resolve()
    records=[]
    if args.only in ['all','profiles']:
        scores=load_scores()
        score_checks={f'D{i+1}':{k:sum((scores['adapted'][t][i]>scores['original'][t][i] if k=='up' else scores['adapted'][t][i]<scores['original'][t][i] if k=='down' else scores['adapted'][t][i]==scores['original'][t][i]) for t in ORDER) for k in ['up','same','down']} for i in range(6)}
        assert all(score_checks[d]=={'up':12,'same':0,'down':0} for d in ['D1','D2','D3','D4'])
        assert score_checks['D5']=={'up':4,'same':6,'down':2}
        for builder,name in [(build_overview,'fig1_overview'),(build_radars,'fig2_radars')]:
            records.append(save(builder(scores),name,[DATA/'six_dimension_scores.csv'],score_checks))
    if args.only in ['all','new','daily']:
        endpoint,states,daily,summary,checks=load_campaigns()
        if args.only != 'daily':
            fig,case_checks=build_case(endpoint,states)
            records.append(save(fig,'fig3_case',[DATA/'case_sequence_properties_r08.json',DATA/'accepted_state_points.csv',DATA/'paired_endpoints.json'],case_checks))
        checks.update(load_original_no_tool(endpoint,summary))
        records.append(save(build_daily(summary),'fig4_daily',[DATA/'daily_values.csv',DATA/'original_no_tool_daily.csv',DATA/'accepted_state_points.csv',DATA/'paired_endpoints.json'],checks))
        (OUT/'audit'/'fig4_pointwise_intervals.json').write_text(json.dumps({a:{m:{q:v.tolist() for q,v in d.items()} for m,d in metrics.items()} for a,metrics in summary.items()},indent=2)+'\n')
        if args.only != 'daily':
            fig,delta_checks=build_quadrants(endpoint)
            records.append(save(fig,'fig5_tool_deltas',[DATA/'paired_endpoints.json'],delta_checks))
    manifest=OUT/'FIGURE_MANIFEST.json'
    old=json.loads(manifest.read_text()).get('figures',[]) if manifest.exists() else []
    permitted=['fig1_overview','fig2_radars','fig3_case','fig4_daily','fig5_tool_deltas']
    by_name={r['figure']:r for r in old+records if r['figure'] in permitted}
    manifest.write_text(json.dumps({'version':'V13.3','created_utc':datetime.now(timezone.utc).isoformat(),
                                   'figures':[by_name[k] for k in permitted if k in by_name]},indent=2)+'\n')


