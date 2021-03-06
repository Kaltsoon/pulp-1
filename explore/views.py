# This file is part of PULP.
#
# PULP is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PULP is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PULP.  If not, see <http://www.gnu.org/licenses/>.

from django.db.models import Q
from django.shortcuts import render

from rest_framework import status
from rest_framework import generics
from rest_framework.response import Response
from rest_framework.views import APIView # class-based views
from rest_framework.decorators import api_view # for function-based views

from explore.models import Article, ArticleTFIDF, Experiment, ExperimentIteration, ArticleFeedback
from explore.serializers import ArticleSerializer
from explore.utils import *

from nltk.stem import SnowballStemmer
from sklearn.preprocessing import normalize
from scipy.sparse.linalg import spsolve

import collections
import sys
import random
import operator
import numpy


DEFAULT_NUM_ARTICLES = 10

#class UserViewSet(viewsets.ModelViewSet):
#    queryset = User.objects.all()
#    serializer_class = UserSerializer

class GetArticle(generics.RetrieveAPIView) :
    queryset = Article.objects.all()
    serializer_class = ArticleSerializer

#class GetArticleOld(APIView) :
#    def get(self, request, article_id) :
#        try :
#            article = Article.objects.get(id=article_id)
#
#        except Article.DoesNotExist :
#            return Response(status=status.HTTP_404_NOT_FOUND)
#
#        serializer = ArticleSerializer(article)
#        return Response(serializer.data)


@api_view(['GET'])
def logout_view(request):
    #logout(request)
    return Response(status=status.HTTP_200_OK)

def get_top_articles_tfidf_old(query_terms, n) :
    """
    return top n articles using tfidf terms,
    ranking articles using okapi_bm25
    """

    try :
        tfidf_query = reduce(operator.or_, [ Q(term=t) for t in query_terms ])
        tfidfs = ArticleTFIDF.objects.select_related('article').filter(tfidf_query)
        #articles = get_top_articles(tfidfs, num_articles)
        #print "%d articles found (%s)" % (len(articles), ','.join([str(a.id) for a in articles]))
    
    except ArticleTFIDF.DoesNotExist :
        print "no articles found containing search terms"
        return []

    tmp = {}

    for tfidf in tfidfs :
        if tfidf.article not in tmp :
            tmp[tfidf.article] = 1.0

        tmp[tfidf.article] *= tfidf.value

    ranking = sorted(tmp.items(), key=lambda x : x[1], reverse=True)

    return [ r[0] for r in ranking[:n] ]

# query terms - a list of stemmed query words
# n - the number of articles to return
def get_top_articles_tfidf(query_terms, n) :
    tfidf = load_sparse_tfidf()
    features = load_features()
    articles = Article.objects.all()

    tmp = {}

    for qt in query_terms :
        if qt not in features :
            continue

        findex = features[qt]

        #print numpy.nonzero(tfidf[:, findex])

        for aindex in numpy.nonzero(tfidf[:, findex])[0] :
            akey = aindex.item()
            if akey not in tmp :
                tmp[akey] = 1.0

            tmp[akey] *= tfidf[aindex,findex]

    ranking = sorted(tmp.items(), key=lambda x : x[1], reverse=True)

    return [ articles[r[0]] for r in ranking[:n] ]

def get_top_articles_linrel(e, linrel_start, linrel_count) :
    X = load_sparse_articles()
    num_articles = X.shape[0]
    num_features = X.shape[1]

    seen_articles = ArticleFeedback.objects.filter(experiment=e).exclude(selected=None)

    X_t = X[ numpy.array([ a.article.id for a in seen_articles ]) ]
    X_tt = X_t.transpose()

    mew = 0.5
    I = mew * scipy.sparse.identity(num_features, format='dia')
    
    W = spsolve((X_tt * X_t) + I, X_tt)
    A = X * W

    Y_t = numpy.matrix([ 1.0 if a.selected else 0.0 for a in seen_articles ]).transpose()

    tmpA = numpy.array(A.todense()) 
    normL2 = numpy.matrix(numpy.sqrt(numpy.sum(tmpA * tmpA, axis=1))).transpose()

    # W * Y_t is the keyword weights
    K = W * Y_t

    tmp = (A * Y_t)
    #I_t = tmp
    I_t = tmp + (0.05 * normL2)
    
    seen_ids = [ a.article.id for a in seen_articles ]
    linrel_ordered = sorted(zip(I_t.transpose().tolist()[0], range(num_articles)), reverse=True)
    top_n = []

    for i in linrel_ordered[linrel_start:] :
        if i[1] not in seen_ids :
            top_n.append(i[1])

        if len(top_n) == linrel_count :
            break

    id2articles = dict([ (a.id, a) for a in Article.objects.filter(pk__in=top_n) ])
    top_articles = [ id2articles[i] for i in top_n ]

    # XXX this is temporary, for experimenting only
    #     and needs to be stored in the database
    stemmer = SnowballStemmer('english')

    used_keywords = collections.defaultdict(list)
    for i in top_articles :
        for word,stem in [ (word,stemmer.stem(word)) for word in i.title.split() + i.abstract.split() ] :
            used_keywords[stem].append(word)

    keyword_stats = {}
    with open('keywords.txt') as f :
        for line in f :
            index,word = line.strip().split()
            if word in used_keywords :
                value = K[int(index),0]**2

                for key in used_keywords[word] :
                    keyword_stats[key] = value
    
    keyword_sum = sum(keyword_stats.values())
    for i in keyword_stats :
        keyword_stats[i] /= keyword_sum

    # XXX this is temporary, value per article
    exploitation = dict(zip(range(num_articles), tmp.transpose().tolist()[0]))
    exploration  = dict(zip(range(num_articles), (0.05 * normL2).transpose().tolist()[0]))

    article_stats = {}

    for i in top_n :
        article_stats[i] = (exploitation[i], exploration[i])

    return top_articles, keyword_stats, article_stats

def get_running_experiments(sid) :
    return Experiment.objects.filter(sessionid=sid, state=Experiment.RUNNING)

def create_experiment(sid, user, num_documents) :
    get_running_experiments(sid).update(state=Experiment.ERROR)

    e = Experiment()

    e.sessionid = sid
    e.number_of_documents = num_documents
    #e.user = user

    e.save()

    return e

def get_experiment(sid) :
    e = get_running_experiments(sid)

    if len(e) != 1 :
        e.update(state=Experiment.ERROR)
        return None

    return e[0]

def create_iteration(experiment, articles) :
    ei = ExperimentIteration()
    ei.experiment = experiment
    ei.iteration = experiment.number_of_iterations
    ei.save()

    for article in articles :
        afb = ArticleFeedback()
        afb.article = article
        afb.experiment = experiment
        afb.iteration = ei
        afb.save()

    return ei

def get_last_iteration(e) :
    return ExperimentIteration.objects.get(experiment=e, iteration=e.number_of_iterations-1)

def add_feedback(ei, articles) :
    feedback = ArticleFeedback.objects.filter(iteration=ei)

    for fb in feedback :
        print "saving clicked=%s for %s" % (str(fb.article.id in articles), str(fb.article.id))
        fb.selected = fb.article.id in articles
        fb.save()

def get_unseen_articles(e) :
    return Article.objects.exclude(pk__in=[ a.article.id for a in ArticleFeedback.objects.filter(experiment=e) ])

@api_view(['GET'])
def textual_query(request) :
    if request.method == 'GET' :
        # experiments are started implicitly with a text query
        # and experiments are tagged with the session id
        request.session.flush()
        #print request.session.session_key

        # get parameters from url
        # q : query string
        if 'q' not in request.GET :
            return Response(status=status.HTTP_404_NOT_FOUND)
        
        query_string = request.GET['q']
        #query_terms = query_string.lower().split()
        
        stemmer = SnowballStemmer('english')
        query_terms = [ stemmer.stem(term) for term in query_string.lower().split() ]

        print "query: %s" % str(query_terms)

        if not len(query_terms) :
            return Response(status=status.HTTP_404_NOT_FOUND)

        # article-count : number of articles to return
        num_articles = int(request.GET.get('article-count', DEFAULT_NUM_ARTICLES))

        print "article-count: %d" % (num_articles)

        # create new experiment
        e = create_experiment(request.session.session_key, None, num_articles) #request.user, num_articles)

        # get documents with TFIDF-based ranking 
        #articles = get_top_articles_tfidf_old(query_terms, num_articles)
        articles = get_top_articles_tfidf(query_terms, num_articles)

        # add random articles if we don't have enough
        fill_count = num_articles - len(articles)
        if fill_count :
            print "only %d articles found, adding %d random ones" % (len(articles), fill_count)
            articles += random.sample(Article.objects.all(), fill_count)
        
        # create new experiment iteration
        # save new documents to current experiment iteration 
        create_iteration(e, articles)
        e.number_of_iterations += 1
        e.save()

        serializer = ArticleSerializer(articles, many=True)
        return Response(serializer.data)

@api_view(['GET'])
def selection_query(request) :
    if request.method == 'GET' :
        # get experiment object
        e = get_experiment(request.session.session_key)
        # get previous experiment iteration
        try :
            ei = get_last_iteration(e)

        except ExperimentIteration.DoesNotExist :
            return Response(status=status.HTTP_404_NOT_FOUND)

        # get parameters from url
        # ?id=x&id=y&id=z
        try :
            selected_documents = [ int(i) for i in request.GET.getlist('id') ]

        except ValueError :
            return Response(status=status.HTTP_404_NOT_FOUND)
        
        print selected_documents

        # add selected documents to previous experiment iteration
        add_feedback(ei, selected_documents)

        # get documents with ML algorithm 
        # remember to exclude all the articles that the user has already been shown
#        all_articles = get_unseen_articles(e)
        rand_articles, keywords, article_stats = get_top_articles_linrel(e, linrel_start=0, linrel_count=e.number_of_documents)

#        print "%d articles left to choose from" % len(all_articles)
        print "%d articles (%s)" % (len(rand_articles), ','.join([str(a.id) for a in rand_articles]))

        # create new experiment iteration
        # save new documents to current experiment iteration
        create_iteration(e, rand_articles)
        e.number_of_iterations += 1
        e.save()

        # response to client
        serializer = ArticleSerializer(rand_articles, many=True)
        article_data = serializer.data
        for i in article_data :
            mean,var = article_stats[i['id']]
            i['mean'] = mean
            i['variance'] = var

        return Response({'articles' : article_data, 'keywords' : keywords})

@api_view(['GET'])
def system_state(request) :
    if request.method == 'GET' :
        e = get_experiment(request.session.session_key)
        try :
            start = request.GET['start']
            count = request.GET['count']
        
        except KeyError :
            return Response(status=status.HTTP_404_NOT_FOUND)

        print "start = %d, count = %d" % (start, count)

        articles, keyword_stats, article_stats = get_top_articles_linrel(e, linrel_start=start, linrel_count=count)
        serializer = ArticleSerializer(articles, many=True)    

        return Response({'article_data' : article_stats, 'keywords' : keyword_stats, 'all_articles' : serializer.data})

@api_view(['GET'])
def end_search(request) :
    if request.method == 'GET' :
        e = get_experiment(request.session.session_key)
        e.state = Experiment.COMPLETE
        e.save()
        return Response(status=status.HTTP_200_OK)

@api_view(['GET'])
def index(request) :
    return render(request, 'index.html')

@api_view(['GET'])
def visualization(request) :
    return render(request, 'visualization.html')

