---
title: python内存泄漏调试
date: 2018-12-22 21:51:30
tags: python, memory leak
---
# 记录一次内存泄漏的调试经历 #

最近写了一个项目，是关于爬虫的，里面涉及到了django作为orm。当时在服务器上运行程序，发现内存占用持续增长，最后直到被系统kill。遇到这个问题，首先要弄清楚内存里面，到底存储了哪些类型的数据。这里主要使用了objgraph，pympler，guppy工具。



## 使用objgraph观察 ##

这里简化下代码, 使用函数dosomething表示一次爬取任务执行。每1分钟执行一次

```python
import objgraph
import schedule


def dosomething():
    objgraph.show_growth()
    # ........ 爬取任务
    pass


schedule.every(1).minutes.do(dosomething)


```

观察如下：

```shell
objects growth
list                          25705    +25705
function                      16416    +16416
tuple                          7941     +7941
dict                           5802     +5802
weakref                        3779     +3779
builtin_function_or_method     2471     +2471
cell                           2303     +2303
type                           2141     +2141
getset_descriptor              1845     +1845
wrapper_descriptor             1542     +1542


objects growth
list                        26225      +520
ObservationList               329       +67
IdentityPartitionCluster      329       +67
SplitResult                    14        +5


objects growth
ObservationList               337        +8
IdentityPartitionCluster      337        +8
SplitResult                    19        +5


objects growth
ObservationList               349        +4
IdentityPartitionCluster      349        +4
```

注意到第一次会出现大幅的增量，是因为第一运行加载类，函数等对象。 从后面的输出结果，可以看到ObservationList和IdentityPartitionCluster一直在持续增长，但是只能看到数量，并不能看到占用内存的数据大小和内容。


## 使用pympler工具 ##

pympler工具可以很容易看到内存的使用情况，用法如下

```python
import objgraph
import schedule
from pympler import tracker, muppy, summary


tr = tracker.SummaryTracker()

def dosomething():
    print "memory total"
    all_objects = muppy.get_objects()
    sum1 = summary.summarize(all_objects)
    summary.print_(sum1)
    
    print "memory difference"
    tr.print_diff()
    # ........ 爬取任务
    pass


schedule.every(1).minutes.do(dosomething)
```

观察输出结果如下：

```shell
memory total
                                     types |   # objects |   total size
========================================== | =========== | ============
                                       str |       94461 |      7.19 MB
                                      dict |        5802 |      6.54 MB
                                   unicode |        9876 |      4.73 MB
                                      type |        2078 |      1.79 MB
                                      code |       14614 |      1.78 MB
                                      list |        9981 |      1.02 MB
  <class 'guppy.heapy.View.ObservationList |         262 |    854.59 KB
                                     tuple |        7941 |    581.80 KB
                                   weakref |        3779 |    324.76 KB
                                       set |         653 |    183.45 KB
                builtin_function_or_method |        2471 |    173.74 KB
                                       int |        6355 |    148.95 KB
                       function (__init__) |        1119 |    131.13 KB
                         getset_descriptor |        1845 |    129.73 KB
                                      cell |        2303 |    125.95 KB
memory difference
                                              types |   # objects |   total size
=================================================== | =========== | ============
                                                str |       72157 |      4.96 MB
                                            unicode |        6943 |      3.18 MB
                                               list |       15319 |      3.13 MB
           <class 'guppy.heapy.View.ObservationList |         262 |    854.59 KB
                                               dict |         590 |    474.83 KB
                                               code |        1991 |    248.88 KB
                                                int |        5817 |    136.34 KB
                                              tuple |         821 |     55.34 KB
                                            weakref |         576 |     49.50 KB
                                               type |          45 |     39.73 KB
  <class 'guppy.heapy.Part.IdentityPartitionCluster |         262 |     24.56 KB
                                 wrapper_descriptor |         164 |     12.81 KB
                                function (__init__) |          95 |     11.13 KB
                                           classobj |         105 |     10.66 KB
                                           instance |         117 |      8.23 KB
                                           



memory total
                       types |   # objects |   total size
============================ | =========== | ============
                     unicode |        9890 |      9.76 MB
                         str |       95090 |      7.22 MB
                        dict |        5802 |      6.54 MB
                        type |        2078 |      1.79 MB
                        code |       14614 |      1.78 MB
                        list |       10501 |      1.08 MB
                       tuple |        7940 |    581.73 KB
                     weakref |        3778 |    324.67 KB
                         set |         653 |    183.45 KB
  builtin_function_or_method |        2471 |    173.74 KB
                         int |        6654 |    155.95 KB
         function (__init__) |        1119 |    131.13 KB
           getset_descriptor |        1845 |    129.73 KB
                        cell |        2302 |    125.89 KB
          wrapper_descriptor |        1542 |    120.47 KB
memory difference
                                              types |   # objects |      total size
=================================================== | =========== | ===============
                                            unicode |          14 |         5.04 MB
                                               list |         520 |        56.93 KB
                                                str |         629 |        35.84 KB
                                                int |         299 |         7.01 KB
  <class 'guppy.heapy.Part.IdentityPartitionCluster |          67 |         6.28 KB
                       <class 'urlparse.SplitResult |           5 |       480     B
                                   _sre.SRE_Pattern |           2 |       176     B
                                  datetime.datetime |           1 |        48     B
                                               cell |          -1 |       -56     B
                                              tuple |          -1 |       -64     B
                                            weakref |          -1 |       -88     B
                                  function (remove) |          -1 |      -120     B
           <class 'guppy.heapy.View.ObservationList |          67 |   -753144     B
           
           
                     
 .......................
 ...................  省略中间多次结果
 .....................
           
           
memory total
                                     types |   # objects |   total size
========================================== | =========== | ============
                                   unicode |       11050 |    385.95 MB
                                       str |       95073 |      7.22 MB
                                      dict |        5802 |      6.54 MB
                                      type |        2078 |      1.79 MB
                                      code |       14614 |      1.78 MB
                                      list |       10501 |      1.08 MB
                                     tuple |        7940 |    581.73 KB
                                   weakref |        3778 |    324.67 KB
                                       int |        8762 |    205.36 KB
                                       set |         653 |    183.45 KB
  <class 'guppy.heapy.View.ObservationList |         897 |    181.98 KB
                builtin_function_or_method |        2471 |    173.74 KB
                       function (__init__) |        1119 |    131.13 KB
                         getset_descriptor |        1845 |    129.73 KB
                                      cell |        2302 |    125.89 KB
memory difference
                                              types |   # objects |   total size
=================================================== | =========== | ============
                                            unicode |           6 |      2.18 MB
                                                str |          29 |      2.14 KB
                                               dict |           0 |    768     B
                       <class 'urlparse.SplitResult |           7 |    672     B
                                  collections.deque |           0 |    512     B
           <class 'guppy.heapy.View.ObservationList |           3 |    336     B
  <class 'guppy.heapy.Part.IdentityPartitionCluster |           3 |    288     B
                                                int |          12 |    288     B
```

可以看到unicode类型，一直在持续增长。从当初4M多，一直持续增长到385M。知道了是unicode的问题，接下来要观察下这些unicode被哪些对象引用。



## 使用guppy工具 ##

guppy可以查看到heap内存的具体使用情况，哪些对象占用多少内存。

```python
import objgraph
import schedule
import guppy
from pympler import tracker, muppy, summary


tr = tracker.SummaryTracker()
hp = guppy.hpy() # 初始化了SessionContext，使用它可以访问heap信息

def dosomething():
    print "heap total"
    heap = hp.heap() # 返回heap内存详情
    references = heap[0].byvia # byvia返回该对象的被哪些引用， heap[0]是内存消耗最大的对象
    print references
    
    # ........ 爬取任务
    pass


schedule.every(1).minutes.do(dosomething)
```

比如上面的代码，返回哪些object引用了unicode这个类型。

观察结果

```shell
Partition of a set of 10151 objects. Total size = 116935280 bytes.
 Index  Count   %     Size   % Cumulative  % Referred Via:
     0    155   2 114401312  98 114401312  98 "[u'sql']"
     1   1245  12  1036064   1 115437376  99 '.func_doc', '[0]'
     2    384   4   282360   0 115719736  99 "['__doc__']"
     3   1456  14   200344   0 115920080  99 '[1]'
     4    144   1    89824   0 116009904  99 '.__doc__', '.func_doc', '[0]'
     5    719   7    89336   0 116099240  99 '[2]'
     6      1   0    72784   0 116172024  99 "['TECHNICAL_500_TEMPLATE']"
     7    565   6    70256   0 116242280  99 '[0]'
     8    532   5    61408   0 116303688  99 '[3]'
     9    448   4    50464   0 116354152 100 '[4]'
<1359 more rows. Type e.g. '_.more' to view.>
```


可以看到sql这个属性，占用了所有unicode的98%的存储空间。继续观察sql这个属性的引用链

```python
import objgraph
import schedule
import guppy
from pympler import tracker, muppy, summary


tr = tracker.SummaryTracker()
hp = guppy.hpy()

def dosomething():
    print "heap total"
    heap = hp.heap()
    references = heap[0].byvia
    print references[0].kind
    print references[0].shpaths
    print references[0].rp
    
    # ........ 爬取任务
    pass


schedule.every(1).minutes.do(dosomething)
```

shpaths返回从最顶端的root到这个object的最短引用路径。rp返回被哪些类型应用信息。

观察结果

```shell
<via "[u'sql']">
 0: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 1: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 2: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 3: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 4: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 5: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 6: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 7: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 8: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
 9: hp.Root.i0_modules['django.contr....models.query'].__dict__['connections'].__dict__['_connections'].??[<weakref...bc7d6830>]['default'].__dict__['queries_log'].??[u'sql']
<... 250 more paths ...>


Reference Pattern by <[dict of] class>.
 0: _ --- [-] 20 <via "[u'sql']">: 0x7f439c90c810, 0x7f439c90c840...
 1: a      [-] 20 dict (no owner): 0x7f439d065b40*2, 0x7f439d54bc58*2...
 2: aa ---- [-] 1 collections.deque: 0x7f43a051d360
 3: a3       [-] 1 dict of django.contrib.gis.db.backends.postgis.base.Databa...
 4: a4 ------ [-] 1 django.contrib.gis.db.backends.postgis.base.DatabaseWrapp...
 5: a5         [-] 1 dict of django.contrib.gis.db.backends.postgis.operation...
 6: a6 -------- [-] 1 django.contrib.gis.db.backends.postgis.operations.PostG...
 7: a7           [^ 3] 1 dict of django.contrib.gis.db.backends.postgis.base....
 8: a4b ------ [-] 1 dict (no owner): 0x7f43bbe00050*1
 9: a4ba        [-] 1 dict (no owner): 0x7f43bbe0de88*1
<Type e.g. '_.more' for more.>
```

可以明显得看到是django的问题，后来上网查了下django内存泄漏，原来是因为django在debug模式下，会保存每一次的sql语句。终于弄清楚了原因，解决办法是把django的settings的debug设置为False。