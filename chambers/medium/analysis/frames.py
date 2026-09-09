"""Per-frame tunnel analysis: heap selTun term lists + JSON wires (constants as nodes)."""
import json, binascii, struct, sys, xml.etree.ElementTree as ET
d = json.load(open('full/VC100.vi.json'))
refmap = {int(k): v for k, v in json.load(open('refmap.json')).items()}
def walk(node, parent=None):
    if isinstance(node, dict):
        yield node, parent
        for k, v in node.items():
            if k in ('body','frames','nodes','children','diagram','cases','tunnels','selector'):
                yield from walk(v, node if 'id' in node else parent)
    elif isinstance(node, list):
        for x in node: yield from walk(x, parent)
nodes, parent_of = {}, {}
for n, p in walk(d['diagram']):
    if 'id' in n:
        nodes[n['id']] = n; parent_of[n['id']] = p['id'] if p else None
# constants may be nested in 'constants' lists? collect any dict with class Constant anywhere
consts = {}
def walk_all(node):
    if isinstance(node, dict):
        yield node
        for v in node.values(): yield from walk_all(v)
    elif isinstance(node, list):
        for x in node: yield from walk_all(x)
for n in walk_all(d['diagram']):
    if n.get('class') == 'Constant' and 'id' in n: consts[n['id']] = n; nodes.setdefault(n['id'], n)
by_from = {}; by_to = {}
def _key(node, term):
    return term if term.isdigit() else (node, term)   # heap uids are unique; named prim terminals are not
for w in d['wires']:
    by_from.setdefault(_key(w['from']['node'], w['from']['terminal']), []).append(w)
    by_to.setdefault(_key(w['to']['node'], w['to']['terminal']), []).append(w)
term_owner = {}
for nid, n in nodes.items():
    for t in n.get('terminals', []) or []: term_owner[t['name']] = (nid, t)
def dec(hexstr):
    try:
        b = binascii.unhexlify(hexstr)
        if len(b) >= 4 and int.from_bytes(b[:4],'big') == len(b)-4: return b[4:].decode('latin1')
        return b.decode('latin1','replace')
    except Exception: return hexstr
STATE = ['Facility Off','Pumping to Rough','Overnight Pump','Engage Turbo','Pumping to High Vac','Disengage Turbo','Vent and Shutdown','Turbo slowing','Venting']
def cval(n):
    v = n.get('value',''); ty = str(n.get('type',''))
    lab = n.get('label') or ''
    if 'Current_Facility_State' in lab or 'Facility_State' in ty or ty.startswith('typedef'):
        try:
            k = int(v[-4:], 16); return f"ST{k}:{STATE[k]}" if k < 9 else v
        except: pass
    if len(v) == 16 and ('Float' in ty or ty.startswith('TypeID')):
        try: return repr(struct.unpack('>d', binascii.unhexlify(v))[0])
        except: pass
    if ty == 'Boolean' or v in ('00','01') or (len(v) <= 2):
        return 'T' if v.endswith('1') and v != '0000' else ('F' if v in ('00','0000','0') else v)
    if v == '0000000000': return '0'
    if len(v) == 10 and v.startswith('00000003'): return '[' + ','.join('T' if x=='1' else 'F' for x in v[8:]) + ']'
    if len(v) == 14 and v.startswith('00000003'): return '[' + ','.join('T' if v[8+2*i:10+2*i]=='01' else 'F' for i in range(3)) + ']'
    if len(v) == 20 and v.startswith('0000000300'): return '[' + ','.join('T' if v[8+4*i:12+4*i]!='0000' else 'F' for i in range(3)) + ']' 
    if len(v) <= 8:
        try: return str(int(v, 16))
        except: return v
    b = binascii.unhexlify(v) if all(c in '0123456789ABCDEFabcdef' for c in v) else b''
    if b[:4] == b'\x00\x00\x00\x03' or b[:4] == b'\x00\x00\x00\x04':  # small boolean array
        return '[' + ','.join('T' if x else 'F' for x in b[4:]) + ']'
    return repr(dec(v))
SRNAME = {'32736': 'SUBSTATE', '24879': 'TARGET', '29716': 'CURRENT', '2548': 'T3_MOTOR', '61338': 'T2_MOTOR'}
def srname(dc): return SRNAME.get(dc, dc)
def refname(n):
    labs = refmap.get(n.get('param_idx'), []); return labs[0] if labs else f"REF[{n.get('param_idx')}]"
# heap: selTun term lists per case
bd = ET.parse('xml/VC100_BDHb.xml').getroot()
byuid = {}
for e in bd.iter():
    if e.get('uid') and (e.get('uid') not in byuid or e.get('class')): byuid[e.get('uid')] = e
# flat-sequence tunnels: term -> (its two terms, mate dco uid);  shift registers: mark
FST = {}; MATE = {}; DCO_TERMS = {}; TERM2DCO = {}; DCO_CLASS = {}
for dco in bd.iter():
    if dco.tag == 'dco' and dco.get('uid') and dco.find('termList') is not None:
        terms = [x.get('uid') for x in dco.find('termList')]
        DCO_TERMS[dco.get('uid')] = terms; DCO_CLASS[dco.get('uid')] = dco.get('class')
        for t in terms: TERM2DCO[t] = dco.get('uid')
        if dco.get('class') == 'flatSeqTun':
            mate = dco.find('mate')
            for t in terms: FST[t] = dco.get('uid')
            if mate is not None: MATE[dco.get('uid')] = mate.get('uid')

# front-panel terminals (fPTerm) -> control label via the FP heap
_fp = ET.parse('xml/VC100_FPHb.xml').getroot()
FPLABEL = {}
for e in _fp.iter():
    if e.get('class') == 'fPDCO' and e.get('uid'):
        lab = None
        for x in e.iter():
            if x.get('class') == 'label':
                t = x.find('.//text')
                if t is not None and t.text: lab = t.text.strip('"'); break
        FPLABEL[e.get('uid')] = lab or f"fp{e.get('uid')}"
FPTERM = {}
for e in bd.iter():
    if e.get('class') == 'fPTerm' and e.get('uid'):
        dco = e.find('dco')
        if dco is not None: FPTERM[e.get('uid')] = FPLABEL.get(dco.get('uid'), f"fpdco{dco.get('uid')}")
def fpname(uid): return f"FP[{FPTERM[uid]}]"


# ---- type table: heap TypeID(n) -> VCTP TopLevel[n + DTHP.IndexShift - 1] -> flat type
_root = ET.parse('xml/VC100.xml').getroot()
_vctp = [e for e in _root.iter() if e.tag == 'VCTP'][0].find('Section')
_FLAT = [c for c in _vctp if c.tag == 'TypeDesc']
_TOP = {int(c.get('Index')): int(c.get('FlatTypeID')) for c in _vctp.find('TopLevel')}
_SHIFT = int([e for e in _root.iter() if e.tag == 'TypeDescSlice'][0].get('IndexShift')) - 1
def heap_type(hid):
    """flat TypeDesc element for a heap TypeID"""
    try: return _FLAT[_TOP[int(hid) + _SHIFT]]
    except Exception: return None
def type_label(hid):
    e = heap_type(hid); return (e.get('Label') if e is not None else None) or f"type{hid}"
def cluster_fields(hid):
    e = heap_type(hid)
    if e is None: return []
    out = []
    for c in e:
        if c.tag == 'TypeDesc' and c.get('TypeID') is not None:
            f = _FLAT[int(c.get('TypeID'))]; out.append(f.get('Label') or f.get('Type'))
    return out
# bundle/unbundle field names: node uid -> {term uid: field name}
FIELDS = {}
for e in bd.iter():
    if e.get('class') in ('nMux', 'nDemux') and e.get('uid'):
        agg = e.find('dcoAgg'); tl = e.find('termList')
        if tl is None: continue
        agg_uid = agg.get('uid') if agg is not None else None
        cl_type = None
        for t in tl:
            dco = t.find('dco')
            if dco is not None and dco.get('uid') == agg_uid:
                td = dco.find('typeDesc'); cl_type = td.text if td is not None else None
        if cl_type and cl_type.startswith('TypeID('):
            hid = cl_type[7:-1]; names = cluster_fields(hid)
            for t in tl:
                dco = t.find('dco')
                if dco is None: continue
                if dco.get('uid') == agg_uid: continue
                i = dco.find('i'); ix = int(i.text) if (i is not None and i.text and i.text.isdigit()) else 0
                if ix < len(names): FIELDS[(e.get('uid'), t.get('uid'))] = names[ix]
            FIELDS[(e.get('uid'), '__cluster__')] = type_label(hid)

def describe_source(term_uid, depth=0, node_id=None):
    """what feeds this terminal (inner tunnel term or node input)"""
    ws = by_to.get(term_uid if str(term_uid).isdigit() else (node_id, term_uid), [])
    if not ws: return '_'
    w = ws[0]; src = w['from']['node']; n = nodes.get(src)
    if n is not None and n.get('class') == 'unknown' and not n.get('terminals'):
        n = None                                          # a structure frame border: treat as tunnel terminal
    if n is None:
        ft = w['from']['terminal']
        if ft in INNER2OUTER and depth < 12:          # value entering a structure: chase to its outer source
            outer, kind = INNER2OUTER[ft]
            if kind in ('lSR','rSR','shiftReg'): return f"SR({srname(TERM2DCO.get(outer, outer))})"
            return describe_source(outer, depth+1)
        if ft in FPTERM: return fpname(ft)
        if ft in TERM2DCO and DCO_CLASS[TERM2DCO[ft]] in ('lSR','rSR','shiftReg'): return f"SR({srname(TERM2DCO[ft])})"
        if ft in TERM2DCO and depth < 14:                # tunnel (flat/stacked sequence, loop, case): other term = input
            dc = TERM2DCO[ft]; kind = DCO_CLASS[dc]
            for c in DCO_TERMS.get(dc, []):
                if c != ft and by_to.get(c): return describe_source(c, depth+1)
            return f"<{kind}-in {ft}>"
        if ft in FPTERM: return fpname(ft)
        return f"<n{src}:{ft}>"
    if n.get('class') == 'Constant': return cval(n)
    if n.get('class') == 'gRef': return refname(n)
    op = n.get('op') or n.get('class')
    if depth > 6: return f"{op}#{src}"
    ins = [t for t in n.get('terminals', []) if t['dir']=='in']
    if op == 'Select':
        b = {t['name']: t for t in ins}
        if {'selector','t_value','f_value'} <= set(b):
            return f"({describe_source(b['selector']['name'],depth+1,src)} ? {describe_source(b['t_value']['name'],depth+1,src)} : {describe_source(b['f_value']['name'],depth+1,src)})"
    if op in ('Less?','Greater?','Equal?','Not Equal?','Less Or Equal?','Greater Or Equal?'):
        b = {t['name']: t for t in ins}
        sym = {'Less?':'<','Greater?':'>','Equal?':'==','Not Equal?':'!=','Less Or Equal?':'<=','Greater Or Equal?':'>='}[op]
        if 'x' in b and 'y' in b: return f"({describe_source(b['x']['name'],depth+1,src)} {sym} {describe_source(b['y']['name'],depth+1,src)})"
    if op == 'Bundle/Unbundle By Name':
        # which output terminal of this node is the wire coming from?
        outt = w['from']['terminal']; fname = FIELDS.get((src, outt))
        cl = FIELDS.get((src, '__cluster__'), '')
        if fname and not any(FIELDS.get((src, t['name'])) for t in ins if t['name'] != ins[0]['name']):
            return f"{describe_source(ins[0]['name'], depth+1, src)}.{fname}"
        parts = []
        for t in ins:
            fn = FIELDS.get((src, t['name']))
            s = describe_source(t['name'], depth+1, src)
            if s == '_': continue
            parts.append(f"{fn}={s}" if fn else s)
        return f"Bundle<{cl}>(" + ', '.join(parts) + ')'
    if op in ('And','Or','Not','Add','Multiply','Subtract','Divide','Exclusive Or','Compound Arithmetic','Index Array','Build Array','Increment','Decrement','Boolean To (0,1)','Concatenate Strings','Bundle/Unbundle By Name','Unbundle','Array To Cluster','And Array Elements','Or Array Elements','Replace Array Subset','Insert Into Array'):
        extra = f"[{n.get('operation')}]" if op == 'Compound Arithmetic' else ''
        return f"{op}{extra}(" + ', '.join(describe_source(t['name'],depth+1,src) for t in ins) + ')'
    if op and op.startswith('Elapsed Time'):
        return f"{op}.out(" + ', '.join(describe_source(t['name'],depth+1,src) for t in ins[:2]) + ')'
    return f"{op}#{src}"
# heap: inner tunnel term -> outer term (all structure kinds)
INNER2OUTER = {}
for e in bd.iter():
    if e.tag == 'SL__arrayElement' and e.get('class') == 'term':
        dco = e.find('dco')
        if dco is None or dco.find('termList') is None: continue
        if dco.get('class') in ('selTun','lpTun','flatSeqTun','seqTun','caseSel','lSR','rSR','shiftReg'):
            outer = e.get('uid')
            for x in dco.find('termList'):
                if x.get('uid') != outer: INNER2OUTER[x.get('uid')] = (outer, dco.get('class'))
def describe_sink(term_uid, node_id=None, depth=0):
    out = []
    for w in by_from.get(term_uid if str(term_uid).isdigit() else (node_id, term_uid), []):
        dst = w['to']['node']; n = nodes.get(dst)
        if n is not None and n.get('class') == 'unknown' and not n.get('terminals'):
            n = None
        if n is None:
            tt = w['to']['terminal']
            if tt in TERM2DCO and DCO_CLASS[TERM2DCO[tt]] in ('rSR','lSR','shiftReg'):
                out.append(f"→SR({srname(TERM2DCO[tt])})")
            elif tt in INNER2OUTER and depth < 8:
                outer, kind = INNER2OUTER[tt]
                out.append(f"⇢({kind}){describe_sink(outer, None, depth+1)}")
            elif tt in TERM2DCO and depth < 8:
                dc = TERM2DCO[tt]; others = [c for c in DCO_TERMS[dc] if c != tt and by_from.get(c)]
                if others: out.append(f"⇢({DCO_CLASS[dc]}){describe_sink(others[0], None, depth+1)}")
                else: out.append(f"⇢({DCO_CLASS[dc]}-end {tt})")
            elif tt in FPTERM:
                out.append(f"→{fpname(tt)}")
            else:
                out.append(f"→<n{dst}:{tt}>")
            continue
        if n.get('class') == 'gRef': out.append(f"→WRITE {refname(n)}")
        else:
            tn = w['to']['terminal']
            nm = next((t.get('name') for t in n.get('terminals',[]) if t['name']==tn), tn)
            out.append(f"→{n.get('op') or n.get('class')}#{dst}.{nm}")
    return ' '.join(out) or '→(nothing)'
def case_tunnels(case_uid):
    c = byuid[case_uid]
    res = []
    for term in c.find('termList'):
        dco = term.find('dco')
        if dco is None: continue
        cls = dco.get('class'); tl = [x.get('uid') for x in dco.find('termList')] if dco.find('termList') is not None else []
        res.append((cls, term.get('uid'), tl))
    return res
def frame_labels(case_id):
    n = nodes[case_id]; return [str(f.get('frame')) for f in n.get('frames', [])]
def analyse(case_id):
    labels = frame_labels(case_id)
    tuns = case_tunnels(case_id)
    print(f"### Case {case_id}: frames {labels}, {len(tuns)} tunnels")
    for cls, outer_term, tl in tuns:
        inner = [u for u in tl if u != outer_term]
        outer_sink = describe_sink(outer_term); outer_src = describe_source(outer_term)
        # direction: output tunnel if the outer term has an outgoing wire
        is_out = bool(by_from.get(outer_term))
        if cls == 'caseSel':
            print(f"  SELECTOR <- {describe_source(outer_term)}"); continue
        if is_out:
            vals = [describe_source(u) for u in inner]
            print(f"  OUT {outer_sink}")
            for lab, v in zip(labels, vals): print(f"       frame {lab:>4}: {v}")
        else:
            print(f"  IN  {outer_src}   (used in frames: {[lab for lab, u in zip(labels, inner) if by_from.get(u)]})")
if __name__ == '__main__':
    for cid in sys.argv[1:]: analyse(cid)

def explain_node(nid, depth_limit=8):
    """print what each *input* of a node is fed by (fresh depth)"""
    n = nodes.get(nid)
    if n is None: print('no node', nid); return
    op = n.get('op') or n.get('class')
    print(f"## node {nid} {op} {n.get('operation') or ''} label={n.get('label')!r}")
    for t in n.get('terminals', []) or []:
        if t['dir'] == 'in':
            print(f"   in  {t['name']:14s} <- {describe_source(t['name'], 0, nid)}")
        else:
            print(f"   out {t['name']:14s} -> {describe_sink(t['name'], nid)}")
