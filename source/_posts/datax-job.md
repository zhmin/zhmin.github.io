---
title: Datax 任务分配原理
date: 2018-12-28 22:14:13
tags: datax, task
---

# Datax 任务分配原理

Datax根首先据配置文件，确定好channel的并发数目。然后将整个job分成一个个小的task，然后划分成组。

## 确定channel数目

如果指定字节数限速，则计算字节限速后的并发数目。如果指定记录数限速，则计算记录数限速后的并发数目。再取两者中最小的channel并发数目。如果两者限速都没指定，则看是否配置文件指定了channel并发数目。

比如以下面这个配置为例：

```json
{
    "core": {
       "transport" : {
          "channel": {
             "speed": {
                "record": 100,
                "byte": 100
             }
          }
       }
    },
    "job": {
      "setting": {
        "speed": {
          "record": 500,
          "byte": 1000,
          "channel" : 1
        }
      }
    }
}
```

首先计算按照字节数限速，channel的数目应该为 500 / 100 = 5

然后按照记录数限速， channel的数目应该为 1000 ／ 100 = 10

最后返回两者的最小值 5。虽然指定了channel为1， 但只有在没有限速的条件下，才会使用。

adjustChannelNumber方法，实现了上述功能

```java
private void adjustChannelNumber() {
    int needChannelNumberByByte = Integer.MAX_VALUE;
    int needChannelNumberByRecord = Integer.MAX_VALUE;

    // 是否指定字节数限速
    boolean isByteLimit = (this.configuration.getInt(
            CoreConstant.DATAX_JOB_SETTING_SPEED_BYTE, 0) > 0);
    if (isByteLimit) {
        // 总的限速字节数
        long globalLimitedByteSpeed = this.configuration.getInt(
                CoreConstant.DATAX_JOB_SETTING_SPEED_BYTE, 10 * 1024 * 1024);

        // 单个Channel的字节数
        Long channelLimitedByteSpeed = this.configuration
                .getLong(CoreConstant.DATAX_CORE_TRANSPORT_CHANNEL_SPEED_BYTE);
        if (channelLimitedByteSpeed == null || channelLimitedByteSpeed <= 0) {
            DataXException.asDataXException(
                    FrameworkErrorCode.CONFIG_ERROR,
                    "在有总bps限速条件下，单个channel的bps值不能为空，也不能为非正数");
        }
        // 计算所需要的channel数目
        needChannelNumberByByte =
                (int) (globalLimitedByteSpeed / channelLimitedByteSpeed);
        needChannelNumberByByte =
                needChannelNumberByByte > 0 ? needChannelNumberByByte : 1;
    }
    // 是否指定记录数限速
    boolean isRecordLimit = (this.configuration.getInt(
            CoreConstant.DATAX_JOB_SETTING_SPEED_RECORD, 0)) > 0;
    if (isRecordLimit) {
        // 总的限速记录数
        long globalLimitedRecordSpeed = this.configuration.getInt(
                CoreConstant.DATAX_JOB_SETTING_SPEED_RECORD, 100000);
        // 获取单个channel的限定的记录数
        Long channelLimitedRecordSpeed = this.configuration.getLong(
                CoreConstant.DATAX_CORE_TRANSPORT_CHANNEL_SPEED_RECORD);
        if (channelLimitedRecordSpeed == null || channelLimitedRecordSpeed <= 0) {
            DataXException.asDataXException(FrameworkErrorCode.CONFIG_ERROR,
                    "在有总tps限速条件下，单个channel的tps值不能为空，也不能为非正数");
        }
        // 计算所需要的channel数目
        needChannelNumberByRecord =
                (int) (globalLimitedRecordSpeed / channelLimitedRecordSpeed);
        needChannelNumberByRecord =
                needChannelNumberByRecord > 0 ? needChannelNumberByRecord : 1;
        LOG.info("Job set Max-Record-Speed to " + globalLimitedRecordSpeed + " records.");
    }

    // 取较小值
    this.needChannelNumber = needChannelNumberByByte < needChannelNumberByRecord ?
            needChannelNumberByByte : needChannelNumberByRecord;

    // 返回最小值
    if (this.needChannelNumber < Integer.MAX_VALUE) {
        return;
    }

    // 是否指定了channel数目
    boolean isChannelLimit = (this.configuration.getInt(
            CoreConstant.DATAX_JOB_SETTING_SPEED_CHANNEL, 0) > 0);
    if (isChannelLimit) {
        this.needChannelNumber = this.configuration.getInt(
                CoreConstant.DATAX_JOB_SETTING_SPEED_CHANNEL);

        LOG.info("Job set Channel-Number to " + this.needChannelNumber
                + " channels.");

        return;
    }

    throw DataXException.asDataXException(
            FrameworkErrorCode.CONFIG_ERROR,
            "Job运行速度必须设置");
}
```



## 切分任务

JobContainer 的split负责将整个job切分成多个task，生成task配置的列表

```java
    private int split() {
        // 计算所需的channel数目
        this.adjustChannelNumber();

        if (this.needChannelNumber <= 0) {
            this.needChannelNumber = 1;
        }
        // 生成任务的reader配置
        List<Configuration> readerTaskConfigs = this
                .doReaderSplit(this.needChannelNumber);
        int taskNumber = readerTaskConfigs.size();
        // 生成任务的writer配置
        List<Configuration> writerTaskConfigs = this
                .doWriterSplit(taskNumber);

        // 生成任务的transformer配置
        List<Configuration> transformerList = this.configuration.getListConfiguration(CoreConstant.DATAX_JOB_CONTENT_TRANSFORMER);

        LOG.debug("transformer configuration: "+ JSON.toJSONString(transformerList));
        
        // 合并任务的reader，writer，transformer配置
        List<Configuration> contentConfig = mergeReaderAndWriterTaskConfigs(
                readerTaskConfigs, writerTaskConfigs, transformerList);
        LOG.debug("contentConfig configuration: "+ JSON.toJSONString(contentConfig));
        
        // 将配置结果保存在job.content路径下
        this.configuration.set(CoreConstant.DATAX_JOB_CONTENT, contentConfig);

        return contentConfig.size();
    }
```

### Reader

doReaderSplit方法， 调用Reader.Job的split方法，返回Reader.Task的Configuration列表

```java
    /**
    * adviceNumber, 建议的数目
    */
    private List<Configuration> doReaderSplit(int adviceNumber) {
        // 切换ClassLoader
        classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
                PluginType.READER, this.readerPluginName));
        // 调用Job.Reader的split切分
        List<Configuration> readerSlicesConfigs =
                this.jobReader.split(adviceNumber);
        if (readerSlicesConfigs == null || readerSlicesConfigs.size() <= 0) {
            throw DataXException.asDataXException(
                    FrameworkErrorCode.PLUGIN_SPLIT_ERROR,
                    "reader切分的task数目不能小于等于0");
        }
        LOG.info("DataX Reader.Job [{}] splits to [{}] tasks.",
                this.readerPluginName, readerSlicesConfigs.size());
        classLoaderSwapper.restoreCurrentThreadClassLoader();
        return readerSlicesConfigs;
    }
```

### Writer

doWriterSplit方法， 调用Writer.JOb的split方法，返回Writer.Task的Configuration列表

```java
    private List<Configuration> doWriterSplit(int readerTaskNumber) {
        // 切换ClassLoader
        classLoaderSwapper.setCurrentThreadClassLoader(LoadUtil.getJarLoader(
                PluginType.WRITER, this.writerPluginName));
        // 调用Job.Reader的split切分
        List<Configuration> writerSlicesConfigs = this.jobWriter
                .split(readerTaskNumber);
        if (writerSlicesConfigs == null || writerSlicesConfigs.size() <= 0) {
            throw DataXException.asDataXException(
                    FrameworkErrorCode.PLUGIN_SPLIT_ERROR,
                    "writer切分的task不能小于等于0");
        }
        LOG.info("DataX Writer.Job [{}] splits to [{}] tasks.",
                this.writerPluginName, writerSlicesConfigs.size());
        classLoaderSwapper.restoreCurrentThreadClassLoader();

        return writerSlicesConfigs;
    }
```

### 合并配置

合并reader，writer，transformer配置列表。并将任务列表，保存在配置job.content的值里。

```java
    private List<Configuration> mergeReaderAndWriterTaskConfigs(
            List<Configuration> readerTasksConfigs,
            List<Configuration> writerTasksConfigs,
            List<Configuration> transformerConfigs) {
        // reader切分的任务数目必须等于writer切分的任务数目
        if (readerTasksConfigs.size() != writerTasksConfigs.size()) {
            throw DataXException.asDataXException(
                    FrameworkErrorCode.PLUGIN_SPLIT_ERROR,
                    String.format("reader切分的task数目[%d]不等于writer切分的task数目[%d].",
                            readerTasksConfigs.size(), writerTasksConfigs.size())
            );
        }

        List<Configuration> contentConfigs = new ArrayList<Configuration>();
        for (int i = 0; i < readerTasksConfigs.size(); i++) {
            Configuration taskConfig = Configuration.newDefault();
            // 保存reader相关配置
            taskConfig.set(CoreConstant.JOB_READER_NAME,
                    this.readerPluginName);
            taskConfig.set(CoreConstant.JOB_READER_PARAMETER,
                    readerTasksConfigs.get(i));
            // 保存writer相关配置
            taskConfig.set(CoreConstant.JOB_WRITER_NAME,
                    this.writerPluginName);
            taskConfig.set(CoreConstant.JOB_WRITER_PARAMETER,
                    writerTasksConfigs.get(i));
            // 保存transformer相关配置
            if(transformerConfigs!=null && transformerConfigs.size()>0){
                taskConfig.set(CoreConstant.JOB_TRANSFORMER, transformerConfigs);
            }

            taskConfig.set(CoreConstant.TASK_ID, i);
            contentConfigs.add(taskConfig);
        }

        return contentConfigs;
    }
```



## 分配任务

### 分配算法

1. 首先根据指定的channel数目和每个Taskgroup的拥有channel数目，计算出Taskgroup的数目
2. 根据每个任务的reader.parameter.loadBalanceResourceMark将任务分组
3. 根据每个任务writer.parameter.loadBalanceResourceMark来讲任务分组
4. 根据上面两个任务分组的组数，挑选出大的那个组
5. 轮询上面步骤的任务组，依次轮询的向各个TaskGroup添加一个，直到所有任务都被分配完

这里举个实例：

目前有7个task，channel有20个，每个Taskgroup拥有5个channel。

首先计算出Taskgroup的数目， 20 ／ 5 =  4 。

根据reader.parameter.loadBalanceResourceMark，将任务分组如下：

```json
{
    "database_a" : [task_id_1, task_id_2],
    "database_b" : [task_id_3, task_id_4, task_id_5],
    "database_c" : [task_id_6, task_id_7]
}
```

根据writer.parameter.loadBalanceResourceMark，将任务分组如下：

```json
{
    "database_dst_d" : [task_id_1, task_id_2],
    "database_dst_e" : [task_id_3, task_id_4, task_id_5, task_id_6, task_id_7]
}
```

因为readerResourceMarkAndTaskIdMap有三个组，而writerResourceMarkAndTaskIdMap只有两个组。从中选出组数最多的，所以这里按照readerResourceMarkAndTaskIdMap将任务分配。

执行过程是，轮询database_a, database_b, database_c，取出第一个。循环上一步

1. 取出task_id_1 放入 taskGroup_1
2. 取出task_id_3 放入 taskGroup_2
3. 取出task_id_6 放入 taskGroup_3
4. 取出task_id_2 放入 taskGroup_4
5. 取出task_id_4 放入 taskGroup_1
6. .........

最后返回的结果为

```json
{
    "taskGroup_1": [task_id_1, task_id_4],
    "taskGroup_2": [task_id_3, task_id_7],
    "taskGroup_3": [task_id_6, task_id_5],
    "taskGroup_4": [task_id_2]
}
```

### 代码解释

任务的分配是由JobAssignUtil类负责。使用者调用assignFairly方法，传入参数，返回TaskGroup配置列表

```java
public final class JobAssignUtil {
    /**
    * configuration 配置
    * channelNumber， channel总数
    * channelsPerTaskGroup， 每个TaskGroup拥有的channel数目
    */
    public static List<Configuration> assignFairly(Configuration configuration, int channelNumber, int channelsPerTaskGroup) {
        
        List<Configuration> contentConfig = configuration.getListConfiguration(CoreConstant.DATAX_JOB_CONTENT);
        // 计算TaskGroup的数目
        int taskGroupNumber = (int) Math.ceil(1.0 * channelNumber / channelsPerTaskGroup);

        ......
        // 任务分组
        LinkedHashMap<String, List<Integer>> resourceMarkAndTaskIdMap = parseAndGetResourceMarkAndTaskIdMap(contentConfig);
        // 调用doAssign方法，分配任务
        List<Configuration> taskGroupConfig = doAssign(resourceMarkAndTaskIdMap, configuration, taskGroupNumber);

        // 调整 每个 taskGroup 对应的 Channel 个数（属于优化范畴）
        adjustChannelNumPerTaskGroup(taskGroupConfig, channelNumber);
        return taskGroupConfig;
    }
}
```

#### 任务分组

按照task配置的reader.parameter.loadBalanceResourceMark和writer.parameter.loadBalanceResourceMark，分别对任务进行分组，选择分组数最高的那组，作为任务分组的源。

```java
    /**
    * contentConfig参数，task的配置列表
    */
    private static LinkedHashMap<String, List<Integer>> parseAndGetResourceMarkAndTaskIdMap(List<Configuration> contentConfig) {
        // reader的任务分组，key为分组的名称，value是taskId的列表
        LinkedHashMap<String, List<Integer>> readerResourceMarkAndTaskIdMap = new LinkedHashMap<String, List<Integer>>();
        // writer的任务分组，key为分组的名称，value是taskId的列表
        LinkedHashMap<String, List<Integer>> 
        writerResourceMarkAndTaskIdMap = new LinkedHashMap<String, List<Integer>>();

        for (Configuration aTaskConfig : contentConfig) {
            int taskId = aTaskConfig.getInt(CoreConstant.TASK_ID);
            
            // 取出reader.parameter.loadBalanceResourceMark的值，作为分组名
            String readerResourceMark = aTaskConfig.getString(CoreConstant.JOB_READER_PARAMETER + "." + CommonConstant.LOAD_BALANCE_RESOURCE_MARK);
            if (readerResourceMarkAndTaskIdMap.get(readerResourceMark) == null) {
                readerResourceMarkAndTaskIdMap.put(readerResourceMark, new LinkedList<Integer>());
            }
            // 把 readerResourceMark 加到 readerResourceMarkAndTaskIdMap 中
            readerResourceMarkAndTaskIdMap.get(readerResourceMark).add(taskId);

            // 取出writer.parameter.loadBalanceResourceMark的值，作为分组名
            String writerResourceMark = aTaskConfig.getString(CoreConstant.JOB_WRITER_PARAMETER + "." + CommonConstant.LOAD_BALANCE_RESOURCE_MARK);
            if (writerResourceMarkAndTaskIdMap.get(writerResourceMark) == null) {
                writerResourceMarkAndTaskIdMap.put(writerResourceMark, new LinkedList<Integer>());
            }
            // 把 writerResourceMark 加到 writerResourceMarkAndTaskIdMap 中
            writerResourceMarkAndTaskIdMap.get(writerResourceMark).add(taskId);
        }

        // 选出reader和writer其中最大的
        if (readerResourceMarkAndTaskIdMap.size() >= writerResourceMarkAndTaskIdMap.size()) {
            // 采用 reader 对资源做的标记进行 shuffle
            return readerResourceMarkAndTaskIdMap;
        } else {
            // 采用 writer 对资源做的标记进行 shuffle
            return writerResourceMarkAndTaskIdMap;
        }
    }
```

### 分配任务

将上一部任务的分组，划分到每个taskGroup里

```java
    private static List<Configuration> doAssign(LinkedHashMap<String, List<Integer>> resourceMarkAndTaskIdMap, Configuration jobConfiguration, int taskGroupNumber) {
        List<Configuration> contentConfig = jobConfiguration.getListConfiguration(CoreConstant.DATAX_JOB_CONTENT);

        Configuration taskGroupTemplate = jobConfiguration.clone();
        taskGroupTemplate.remove(CoreConstant.DATAX_JOB_CONTENT);

        List<Configuration> result = new LinkedList<Configuration>();

        // 初始化taskGroupConfigList
        List<List<Configuration>> taskGroupConfigList = new ArrayList<List<Configuration>>(taskGroupNumber);
        for (int i = 0; i < taskGroupNumber; i++) {
            taskGroupConfigList.add(new LinkedList<Configuration>());
        }

        // 取得resourceMarkAndTaskIdMap的值的最大个数
        int mapValueMaxLength = -1;

        List<String> resourceMarks = new ArrayList<String>();
        for (Map.Entry<String, List<Integer>> entry : resourceMarkAndTaskIdMap.entrySet()) {
            resourceMarks.add(entry.getKey());
            if (entry.getValue().size() > mapValueMaxLength) {
                mapValueMaxLength = entry.getValue().size();
            }
        }

        
        int taskGroupIndex = 0;
        // 执行mapValueMaxLength次数，每一次轮询一遍resourceMarkAndTaskIdMap
        for (int i = 0; i < mapValueMaxLength; i++) {
            // 轮询resourceMarkAndTaskIdMap
            for (String resourceMark : resourceMarks) {
                if (resourceMarkAndTaskIdMap.get(resourceMark).size() > 0) {
                    // 取出第一个
                    int taskId = resourceMarkAndTaskIdMap.get(resourceMark).get(0);
                    // 轮询的向taskGroupConfigList插入值
                    taskGroupConfigList.get(taskGroupIndex % taskGroupNumber).add(contentConfig.get(taskId));
                    // taskGroupIndex自增
                    taskGroupIndex++;
                    // 删除第一个
                    resourceMarkAndTaskIdMap.get(resourceMark).remove(0);
                }
            }
        }

        Configuration tempTaskGroupConfig;
        for (int i = 0; i < taskGroupNumber; i++) {
            tempTaskGroupConfig = taskGroupTemplate.clone();
            // 设置TaskGroup的配置
            tempTaskGroupConfig.set(CoreConstant.DATAX_JOB_CONTENT, taskGroupConfigList.get(i));
            tempTaskGroupConfig.set(CoreConstant.DATAX_CORE_CONTAINER_TASKGROUP_ID, i);

            result.add(tempTaskGroupConfig);
        }
        // 返回结果
        return result;
    }
```

#### 为组分配channel

上面已经把任务划分成多个组，为了每个组能够均匀的分配channel，还需要调整。算法原理是，当channel总的数目，不能整除TaskGroup的数目时。多的余数个channel，从中挑选出余数个TaskGroup，每个多分配一个。

比如现在有13个channel，然后taskgroup确有5个。那么首先每个组先分 13 / 5 = 2 个。那么还剩下多的3个chanel，分配给前面个taskgroup。

```java
    private static void adjustChannelNumPerTaskGroup(List<Configuration> taskGroupConfig, int channelNumber) {
        int taskGroupNumber = taskGroupConfig.size();
        int avgChannelsPerTaskGroup = channelNumber / taskGroupNumber;
        int remainderChannelCount = channelNumber % taskGroupNumber;
        // 表示有 remainderChannelCount 个 taskGroup,其对应 Channel 个数应该为：avgChannelsPerTaskGroup + 1；
        // （taskGroupNumber - remainderChannelCount）个 taskGroup,其对应 Channel 个数应该为：avgChannelsPerTaskGroup

        int i = 0;
        for (; i < remainderChannelCount; i++) {
            taskGroupConfig.get(i).set(CoreConstant.DATAX_CORE_CONTAINER_TASKGROUP_CHANNEL, avgChannelsPerTaskGroup + 1);
        }

        for (int j = 0; j < taskGroupNumber - remainderChannelCount; j++) {
            taskGroupConfig.get(i + j).set(CoreConstant.DATAX_CORE_CONTAINER_TASKGROUP_CHANNEL, avgChannelsPerTaskGroup);
        }
    }
```