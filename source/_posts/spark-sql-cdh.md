---
title: CDH 集群添加 Spark Sql 命令行
date: 2019-04-03 21:37:32
tags: spark, cdh
categories: spark sql
---

# Cloudera 集群 添加 Spark Sql 命令行

目前需要从Hive Sql 迁移到 Spark Sql，但是CDH版本的集群，不支持Spark Sql命令行。这篇文章详细介绍了Cloudera的Spark命令是如何启动的，然后在此基础上添加Spark Sql。

## Cloudera Spark 启动原理

CDH的版本目前是5.12，它的内置Spark版本是1.6。不过我已经添加了Spark2的版本，所以对于spark sql的支持，下面讲解的也只是针对spark2。如果Spark的版本号不同，对应的原理也是一样。

我们通过以spark2-submit命令为例，查看它的启动过程。

首先查看该命令的位置，可以看到最后链接指向了/opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/bin/spark2-submit。

```shell
[root@master01 ~]# which spark2-submit
/bin/spark2-submit

[root@master01 ~]# ll /bin/spark2-submit
lrwxrwxrwx 1 root root 31 Feb 15 10:44 /bin/spark2-submit -> /etc/alternatives/spark2-submit

[root@master01 ~]# ll /etc/alternatives/spark2-submit
lrwxrwxrwx 1 root root 84 Feb 15 10:44 /etc/alternatives/spark2-submit -> /opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/bin/spark2-submit
```

接下来看看spark2-submit文件的内容

```shell
#!/bin/bash
  # Reference: http://stackoverflow.com/questions/59895/can-a-bash-script-tell-what-directory-its-stored-in
  SOURCE="${BASH_SOURCE[0]}" # 获取shell文件路径
  BIN_DIR="$( dirname "$SOURCE" )" # 获取该shell文件的所在文件夹
  # 从上面的代码可以看到，这里都使用了链接的方法。 -h 选项 是测试该文件是否为链接
  while [ -h "$SOURCE" ]
  do
    SOURCE="$(readlink "$SOURCE")" # 使用readlink方法，获取该文件的绝对路径
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE" # 检查文件路径是否为根目录
    BIN_DIR="$( cd -P "$( dirname "$SOURCE"  )" && pwd )"  # 
  done
  BIN_DIR="$( cd -P "$( dirname "$SOURCE" )" && pwd )" # 进入到文件的目录，这里的值为/opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/bin/
  CDH_LIB_DIR=$BIN_DIR/../../CDH/lib # 这里的值为/opt/cloudera/parcels/CDH/lib
  LIB_DIR=$BIN_DIR/../lib # 这里的值为/opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/lib
export HADOOP_HOME=$CDH_LIB_DIR/hadoop # 设置HADOOP_HOME环境变量为/opt/cloudera/parcels/CDH/lib/hadoop

# 查看JAVA_HOME环境变量
. $CDH_LIB_DIR/bigtop-utils/bigtop-detect-javahome

exec $LIB_DIR/spark2/bin/spark-submit "$@" # 执行命令
```



spark2-submit命令会设置好环境变量，并且进入到对应的执行目录里。最后执行了/opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/lib/spark2/bin/spark-submit文件，下面看看这个文件的内容

```shell
#!/usr/bin/env bash
# 寻找环境变量SPARK_HOME的值
if [ -z "${SPARK_HOME}" ]; then
  source "$(dirname "$0")"/find-spark-home
fi

# disable randomized hash for string in Python 3.3+
export PYTHONHASHSEED=0
# 执行java命令，main类为org.apache.spark.deploy.SparkSubmit
exec "${SPARK_HOME}"/bin/spark-class org.apache.spark.deploy.SparkSubmit "$@"
```

上面使用了find-spark-home文件，查看SPARK_HOME的值。如果存在SPARK_HOME存在，则直接返回。如果不返回， 则查看当前目录下，是否有find_spark_home.py文件。如果存在find_spark_home.py文件，则调用python执行获取结果。如果不存在，则使用当前目录为SPARK_HOME。

这里Cloudera的最后使用/opt/cloudera/parcels/SPARK2/lib/spark2目录，作为SPARK_HOME的值。



## 添加 Spark Sql 命令

首先我们添加 /bin 目录下添加 spark2-sql 链接文件，这样 spark2-sql 命令就可以被找到

```shell
ln -s  /etc/alternatives/spark2-sql /bin/spark2-sql
ln -s  /opt/cloudera/parcels/SPARK2/bin/spark2-sql  /etc/alternatives/spark2-sql
```

创建文件 /opt/cloudera/parcels/SPARK2/bin/spark2-sql，文件内容如下

```shell
#!/bin/bash
  # Reference: http://stackoverflow.com/questions/59895/can-a-bash-script-tell-what-directory-its-stored-in
  SOURCE="${BASH_SOURCE[0]}"
  BIN_DIR="$( dirname "$SOURCE" )"
  while [ -h "$SOURCE" ]
  do
    SOURCE="$(readlink "$SOURCE")"
    [[ $SOURCE != /* ]] && SOURCE="$DIR/$SOURCE"
    BIN_DIR="$( cd -P "$( dirname "$SOURCE"  )" && pwd )"
  done
  BIN_DIR="$( cd -P "$( dirname "$SOURCE" )" && pwd )"
  CDH_LIB_DIR=$BIN_DIR/../../CDH/lib
  LIB_DIR=$BIN_DIR/../lib
export HADOOP_HOME=$CDH_LIB_DIR/hadoop

# Autodetect JAVA_HOME if not defined
. $CDH_LIB_DIR/bigtop-utils/bigtop-detect-javahome

exec $LIB_DIR/spark2/bin/spark-sql "$@"  
```

并且给与这个文件可执行权限 `chmod a+x /opt/cloudera/parcels/SPARK2/bin/spark2-sql`

然后创建 /opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/lib/spark2/bin/spark-sql 文件，内容如下

```shell
#!/usr/bin/env bash

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

if [ -z "${SPARK_HOME}" ]; then
  source "$(dirname "$0")"/find-spark-home
fi

export _SPARK_CMD_USAGE="Usage: ./bin/spark-sql [options] [cli option]"
exec "${SPARK_HOME}"/bin/spark-submit --class org.apache.spark.sql.hive.thriftserver.SparkSQLCLIDriver "$@"
```

同样给与这个文件可执行权限 `chmod a+x /opt/cloudera/parcels/SPARK2-2.2.0.cloudera3-1.cdh5.13.3.p0.556753/lib/spark2/bin/spark-sql `



这里将相关命令的文件，都已经配置好了。但是因为Cloudera内置的Spark库不全，需要将缺少的依赖包添加进来。我们可以直接下载编译完成的Spark库，下载网址为  http://archive.apache.org/dist/spark/ 。从里面选取的版本号，需要与Cloudera上安装的Spark版本一致。

下载完并且解压后，里面会有一个 jars 的目录，将Cloudera缺少的库，拷贝到cloudera对应的库文件夹里面即可。

```shell
cp jars/spark-hive-thriftserver_2.11-2.2.0.jar /opt/cloudera/parcels/SPARK2/lib/spark2/jars/
cp jars/hive-* /opt/cloudera/parcels/SPARK2/lib/spark2/jars/ # 注意到这里不要覆盖原有文件
```

整个添加的过程就完成了，接下来就可以直接使用spark-sql命令，就可以进入客户端shell

```shell
sudo -u hive spark2-sql
spark-sql> use test;
spark-sql> show tables;
.....
```

下面简单介绍下spark-sql常用的命令选项

```shell
# 执行 sql 命令
sudo -u hive spark2-sql -e "select count(*) from test.my_table;"

# 执行 sql 文件
sudo -u hive spark2-sql -f my_sql.sql
```



