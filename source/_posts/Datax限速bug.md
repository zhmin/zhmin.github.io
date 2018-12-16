---
title: Datax限速bug
date: 2018-12-16 14:40:20
tags: datax
---

# Datax 限速原理 #

## 使用 ##

官方文档并没有写到怎么配置限速，通过观察源码才得知，需要配置core.transport.channel.speed.byte或core.transport.channel.speed.record值。这两个值是指定每个channel的速度。注意job.setting.speed.byte和job.setting.speed.record也是指定速度，但这个是限制所有channel的总速度。

我们使用限速功能时，可以通过制定job.setting.speed和core.transport.channel.speed的值，这样datax会自动的计算出channel数目。也可以指定core.transport.channel.speed和channel数目。

配置文件示例如下，指定了channel的数目为5，每个channel的数据读取条数为100：

```json
{
    "core": {
         "transport" : {
              "channel": {
                   "speed": {
                       "record": 100
                    }
               }
         }
    },
    "job": {
        "setting": {
            "speed": {
                "channel": 5,
            }
        },
        "content": [
            {
                "reader": {
                    "name": "sqlserverreader",
                    "parameter": {

                        "username": "xx",
                        "password": "xx",
                        "connection": [
                            {
                                "querySql": [
                                    "SELECT * from mytable LIMIT 100 "
                                ],
                                "jdbcUrl": [
                                    "jdbc:postgresql://host:port/database"
                                ]
                            }
                        ]
                    }
                },
               "writer": {
                    "name": "streamwriter",
                    "parameter": {
                        "print": false,
                        "encoding": "UTF-8"
                    }
                }
            }
        ]
    }
}
```



## Bug ##

当指定了ob.setting.speed配置，是必须同时指定core.transport.channel.speed的配置，但是目前datax却没有抛出错误。这里可以看看datax的源码，是如何检查值的。

```
public class JobContainer extends AbstractContainer {
	private void adjustChannelNumber() {
        int needChannelNumberByByte = Integer.MAX_VALUE;
        int needChannelNumberByRecord = Integer.MAX_VALUE;
        
	    // 是否配置了总的速度限制job.setting.speed.byte
        boolean isByteLimit = (this.configuration.getInt(
                CoreConstant.DATAX_JOB_SETTING_SPEED_BYTE, 0) > 0);
        if (isByteLimit) {
            long globalLimitedByteSpeed = this.configuration.getInt(
                    CoreConstant.DATAX_JOB_SETTING_SPEED_BYTE, 10 * 1024 * 1024);

            // CoreConstant.DATAX_CORE_TRANSPORT_CHANNEL_SPEED_BYTE在默认文件core.json中指定了，
            // 默认为-1
            Long channelLimitedByteSpeed = this.configuration
                    .getLong(CoreConstant.DATAX_CORE_TRANSPORT_CHANNEL_SPEED_BYTE);
            if (channelLimitedByteSpeed == null || channelLimitedByteSpeed <= 0) {
                /* 如果没有指定core.transport.channel.speed.byte， 则这里本应该抛出错误
                   但是这里DataXException.asDataXException仅仅是返回了异常，并没有throw
                */
                DataXException.asDataXException(
                        FrameworkErrorCode.CONFIG_ERROR,
                        "在有总bps限速条件下，单个channel的bps值不能为空，也不能为非正数");
            }

            needChannelNumberByByte =
                    (int) (globalLimitedByteSpeed / channelLimitedByteSpeed);
            needChannelNumberByByte =
                    needChannelNumberByByte > 0 ? needChannelNumberByByte : 1;
            LOG.info("Job set Max-Byte-Speed to " + globalLimitedByteSpeed + " bytes.");
        }

        boolean isRecordLimit = (this.configuration.getInt(
                CoreConstant.DATAX_JOB_SETTING_SPEED_RECORD, 0)) > 0;
        if (isRecordLimit) {
            long globalLimitedRecordSpeed = this.configuration.getInt(
                    CoreConstant.DATAX_JOB_SETTING_SPEED_RECORD, 100000);
            Long channelLimitedRecordSpeed = this.configuration.getLong(
                    CoreConstant.DATAX_CORE_TRANSPORT_CHANNEL_SPEED_RECORD);
            if (channelLimitedRecordSpeed == null || channelLimitedRecordSpeed <= 0) {
                throw DataXException.asDataXException(FrameworkErrorCode.CONFIG_ERROR,
                        "在有总tps限速条件下，单个channel的tps值不能为空，也不能为非正数");
            }

            needChannelNumberByRecord =
                    (int) (globalLimitedRecordSpeed / channelLimitedRecordSpeed);
            needChannelNumberByRecord =
                    needChannelNumberByRecord > 0 ? needChannelNumberByRecord : 1;
            LOG.info("Job set Max-Record-Speed to " + globalLimitedRecordSpeed + " records.");
        }

        ........
    }
        
```

可以看到Datax是检查了配置参数，但是这里没有抛出异常，所以需要在DataXException.asDataXException的前面加上throw，将异常抛出来。否则仅仅指定了job.setting.speed，是没有任何限速作用的。

## 优化 ##

Datax的限速原理，是它会每隔一段时间，检查速度。如果速度过快，就会sleep一段时间，来把速度降下来。这种限速其实不太精确，可以自己改写代码，使用guava库的RateLimiter来实现精确的限速。

