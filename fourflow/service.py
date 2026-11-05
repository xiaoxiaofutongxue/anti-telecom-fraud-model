from .core import build_case_graph,parse_time
import networkx as nx
import hashlib
import json


def review_signature(reviews):
    return hashlib.sha256(json.dumps(sorted(reviews,key=lambda r:r['finding_id']),sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def analysis_graph(dataset,case_id,store,as_of=None,include_pending=False):
    graph=build_case_graph(dataset,case_id,as_of=as_of,include_unverified=True)
    reviews={r["finding_id"]:r for r in store.reviews(dataset.fingerprint,case_id,as_of)}
    graph.graph['review_signature']=review_signature(list(reviews.values()))
    cutoff=parse_time(as_of) if as_of else None
    remove=[]
    for u,v,key,data in graph.edges(keys=True,data=True):
        review=reviews.get("identity:"+data["record_id"]) if data["relation"]=="identity" else None
        if review:
            data['source_verified']=data.get('verified')
            data["verified"]="verified" if review['status']=='已核实线索' else 'pending'
            data['effective_from']=max(parse_time(data['timestamp']),parse_time(review['updated'])).isoformat()
            data["reviewer"]=review["reviewer"]; data["review_note"]=review["note"]; data["review_time"]=review["updated"]
            data['review_status']=review['status']
        if data.get('verified')=='pending' and not include_pending:
            remove.append((u,v,key))
    graph.remove_edges_from(remove)
    graph.remove_nodes_from(list(nx.isolates(graph)))
    return graph


def identity_states(dataset,graph,store):
    cutoff=graph.graph.get('as_of') or None
    reviews={r['finding_id']:r for r in store.reviews(dataset.fingerprint,graph.graph['case_id'],cutoff)}
    effective={d['record_id'] for u,v,k,d in graph.edges(keys=True,data=True) if d['relation']=='identity' and d.get('verified')=='verified'}
    rows=[]
    for row in dataset.rows('identity_links',graph.graph['case_id']):
        if cutoff and row['_time']>parse_time(cutoff):continue
        rows.append({**{k:v for k,v in row.items() if not k.startswith('_')},'effective_verified':row['link_id'] in effective,'manual_review':reviews.get('identity:'+row['link_id']) or ''})
    return rows
