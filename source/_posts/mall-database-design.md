---
title: 数据库表设计思路
date: 2020-08-24 23:01:26
tags: 项目感悟
categories: project
---



## 前言

最近开发了一个电商项目，我负责的数据库的表设计，经过这个项目的锻炼，增长了我的数据库设计能立，这篇文章主要讲讲电商的常见场景设计，同时也讲下设计经验。



## 商品 Sku 设计

在电商系统里，对于商品有两个重要的概念 SPU 和 SKU。举个例子，苹果手机 iphone 6 就是 SPU，它指定了这个商品。苹果 iphone 6 有着不同的颜色，有黑色，白色，金色，每种颜色对应一个 SKU。可以概括为一个 SPU 包含了 多个 SKU，只有 SKU 有着库存和价格信息。

在了解了基本概念后，我们该如何设计数据库呢。首先建立 SPU 和 SKU 两个表，关系是一对多。因为 SKU 包含了不同的规格，所以还需要建立规格表，关系是一对多。

### 表设计

{% plantuml %}

@startuml
	spu  ||--o{ sku 
	sku  ||--o{ atribute
@enduml

{% endplantuml %}

商品 spu 表

| 列名     | 类型   | 描述         |
| -------- | ------ | ------------ |
| name     | string | 商品名称     |
| category | string | 分类名称     |
| image    | string | 商品详情图片 |



商品 sku 表

| 列名   | 类型 | 描述   |
| ------ | ---- | ------ |
| spu_id | int  | spu id |
| price  | int  | 价格   |
| stock  | int  | 存库   |



商品规格表 atribute

| 列名   | 类型   | 描述     |
| ------ | ------ | -------- |
| sku_id | int    | sku id   |
| name   | string | 规格名称 |
| value  | string | 规格值   |



### 设计优化

在表设计的时候，我们还需要结合业务才能更好的优化。上述的表格设计得非常规范，但是会造成多次查询的性能影响。sku 信息的展示，一般只在商品详情页，用户在选择商品规则时才会用到，所以这里将商品规则表删除，将它直接保存到 sku 表。

商品 sku 表

| 列名      | 类型 | 描述 |
| --------- | ---- | ---- |
| spu_id    |      |      |
| price     | int  | 价格 |
| stock     | int  | 存库 |
| attribute | json | 规格 |

这里新增了一列 attribute，类型为 Map，它的 key 值为商品规格名称，值为规格值。这样设计后，我们只需要查找 sku 表一次就可以了。有时候合理的使用 json 数据，会使得表的数量更加少，结构更加简洁。



## 订单设计

### 设计需求

1. 用户在同一订单里，购买多种商品，并且支持一次性付款

2. 用户在购买商品时，能够使用优惠券或者参与活动，达到优惠的目的

   

### 表设计

{% plantuml %}

@startuml
	Order ||--o{ OrderItem
	Logistics ||--o{ OrderItem
	Order ||--o{ OrderDiscount
@enduml

{% endplantuml %}



Order 表示用户订单，里面包含了付款的金额和付款状态

| **列名**   | **类型** | **含义**            |
| ---------- | -------- | ------------------- |
| order_id   | string   | 订单 id，由系统生成 |
| amount     | int      | 实际付款金额        |
| payType    | string   | 支付类型            |
| status     | string   | 付款状态            |
| account_id | int      | 用户 id             |



OrderItem 记录了此次订单内的商品

| **列名**     | **类型** | **含义**    |
| ------------ | -------- | ----------- |
| order_id     | string   | 订单 id     |
| goods_sku_id | string   | 商品 sku id |
| number       | int      | 商品数量    |
| price        | int      | 购买单价    |



OrderDiscount 记录了此次付款，使用的优惠和抵扣情况。

| **列名**      | **类型** | **含义**                                     |
| ------------- | -------- | -------------------------------------------- |
| order_id      | string   | 订单 id                                      |
| resource_type | string   | 使用资源类型（比如优惠券，活动，积分抵扣等） |
| resource_id   | string   | 资源 id，当是优惠券时，它会指定是哪种优惠券  |
| cost          | int      | 消耗资源数                                   |
| discount      | int      | 优惠金额                                     |



这里需要注意下 OrderDiscount 表，它记录了订单的优惠数据。这里并没有局限哪种优惠类型，而是使用 resource_type 字段来表示优惠类型，使用 cost 表示消耗的资源数量。如果用户使用了优惠券和参加了活动，那么就会有两条优惠记录，resource_id 分别表示优惠券的 id 和 活动 id。这种设计并没采用外键的方式，这样扩展性更强，当有其它优惠类型时，比如积分，那么只需要添加一行数据，它的 resource_type 为积分即可。



## 物流信息设计

 当用户支付订单后，我们需要给他们发货。有时候一个订单包含了多件商品，有些商品会合并成一个快递发送，有的则需要单独发送。很明显这是一个一对多的关系，设计的表如下：

OrderItem 表增加 logistics_id 字段

| **列名**     | **类型** | **含义**    |
| ------------ | -------- | ----------- |
| order_id     | string   | 订单 id     |
| goods_sku_id | string   | 商品 sku id |
| number       | int      | 商品数量    |
| price        | int      | 购买单价    |
| logistics_id | int      | 物流信息 id |



Logistics 表

| **列名**         | **类型** | **含义**                     |
| ---------------- | -------- | ---------------------------- |
| order_id         | string   | 订单 id                      |
| receiver_name    | string   | 收件人姓名                   |
| receiver_phone   | string   | 收件人电话                   |
| receiver_address | string   | 收件人地址                   |
| status           | string   | 物流状态（在途，签收，拒签） |



但是上面这种表设计，将两者的关系绑定固定为一对多，如果发现了特殊情况，比如某个商品购买了两个，因为库存不足，前期只发货了一个，第二个需要后面才能补发，那么会造成一种商品对应两个物流信息，这样原有的表设计就不能保证这一点。

我们再来回顾下订单的发货业务，将基本的原理提取出来，就是一个商品肯定会对应一个物流。然后在此基础之上，再来慢慢优化。因为物流信息相对订单商品表，变化会更加频繁，比如用户填错了订单地址，需要客服帮忙修改。所以我重新设计成了下一版，

OrderItem 表去除 logistics_id 字段

Logistics 表

| **列名**         | **类型** | **含义**                     |
| ---------------- | -------- | ---------------------------- |
| order_id         | string   | 订单 id                      |
| receiver_name    | string   | 收件人姓名                   |
| receiver_phone   | string   | 收件人电话                   |
| receiver_address | string   | 收件人地址                   |
| status           | string   | 物流状态（在途，签收，拒签） |
| order_items      | json     | 订单商品信息                 |



order_items 字段如下所示，下面表示该快递包含了 goods_sku_id_1 和 goods_sku_id_2 两种商品，其中 goods_sku_id_1 商品的数量为 2 个。

```json
{
    "goods_sku_id_1": 2,
    "goods_sku_id_2": 1
}
```



这种设计并没有使用外键来表示一对多的关系，而是都是通过订单 id 和 商品就可以查询对应的物流信息，结合 json 字段使得表的设计更加灵活。所以在实际项目中，多思考下业务也能对数据库的设计有很大的帮助。



## 总结

上面介绍了商的一些场景城设计，可以看到数据库的设计也是很灵活的。表之间的关系不仅可以通过外键来表示，还可以使用 json 字段表示。此外对业务的深入理解，可以帮助表的设计更加简洁，更有扩展性。