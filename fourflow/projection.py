"""四种节点特征探索；投影不替代独立模型评估，不自动合并身份。"""
import numpy as np

from .core import build_case_graph,entity_id,is_true
from .model import encode_graph,FEATURE_NAMES
from .investigation import pca_projection


def project_graph(dataset,graph,method='PCA'):
    if method=='PCA':return pca_projection(graph)
    x,_=encode_graph(graph);nodes=sorted(graph);z=(x-x.mean(axis=0))/np.maximum(x.std(axis=0),.1)
    metadata={}
    if method=='LDA':
        from sklearn.discriminant_analysis import LinearDiscriminantAnalysis
        labelled=[];labels=[]
        for case in dataset.tables['cases']:
            rows=[r for r in dataset.snapshot_rows('person_labels',case['case_id'],graph.graph.get('as_of') or None) if is_true(r['confirmed'])]
            if not rows:continue
            other=build_case_graph(dataset,case['case_id'],as_of=graph.graph.get('as_of') or None)
            if not other or len(other)>256:continue
            raw,_=encode_graph(other);index={n:i for i,n in enumerate(sorted(other))}
            for row in rows:
                node=entity_id('actor',row['actor_id'])
                if node in index:labelled.append(raw[index[node]]);labels.append(row['role_label'])
        if len(set(labels))<2 or len(labels)<=len(set(labels)):raise ValueError('LDA需要至少两类经核实人员标签，且样本数大于类别数；未标注节点不当作负类')
        fit=np.array(labelled);mean=fit.mean(axis=0);scale=np.maximum(fit.std(axis=0),.1)
        model=LinearDiscriminantAnalysis(solver='svd',n_components=min(2,len(set(labels))-1))
        model.fit((fit-mean)/scale,labels)
        indices=[i for i,n in enumerate(nodes) if graph.nodes[n]['kind']=='actor'];nodes=[nodes[i] for i in indices]
        if not nodes:raise ValueError('当前图没有人员节点可作LDA探索')
        coordinates=model.transform((x[indices]-mean)/scale)
        metadata={'fit_labelled_persons':len(labels),'supervised_visualization':True,'fit_scope':'当前加载数据中在截止时间可用的核实角色标签，仅用于探索，不是独立分类评估'}
    elif method=='LLE':
        from sklearn.manifold import LocallyLinearEmbedding
        if len(nodes)<4:raise ValueError('LLE至少需要4个节点')
        model=LocallyLinearEmbedding(n_neighbors=min(10,len(nodes)-1),n_components=2,reg=1e-3,eigen_solver='dense',random_state=42)
        coordinates=model.fit_transform(z);metadata={'n_neighbors':model.n_neighbors,'reconstruction_error':float(model.reconstruction_error_)}
    elif method=='t-SNE':
        from sklearn.manifold import TSNE
        if len(nodes)<4:raise ValueError('t-SNE至少需要4个节点')
        perplexity=min(30.,max(2.,(len(nodes)-1)/3))
        model=TSNE(n_components=2,perplexity=perplexity,random_state=42,init='pca',learning_rate='auto',max_iter=750,n_jobs=1)
        coordinates=model.fit_transform(z);metadata={'perplexity':perplexity,'kl_divergence':float(model.kl_divergence_),'seed':42}
    else:raise ValueError('投影方法须为PCA、LDA、LLE或t-SNE')
    if coordinates.ndim!=2 or coordinates.shape[1]==0 or not np.isfinite(coordinates).all():raise ValueError('投影退化或数值无效，请复核特征与标签')
    return {'method':method,'feature_names':FEATURE_NAMES,'points':[{'node':n,'pc1':float(coordinates[i,0]),'pc2':float(coordinates[i,1]) if coordinates.shape[1]>1 else 0.} for i,n in enumerate(nodes)],**metadata,'interpretation':'仅用于特征探索；二维距离不表示同一人或团伙；已知标签投影的分离程度不是独立预测性能。'}
