import {
  CfnOutput,
  CfnParameter,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps
} from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import type { Construct } from 'constructs';

export class DataStack extends Stack {
  readonly cluster: rds.DatabaseCluster;
  readonly databaseSecurityGroup: ec2.SecurityGroup;
  readonly runtimeBucket: s3.Bucket;
  readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props?: StackProps) {
    super(scope, id, props);
    const s3PrefixListId = new CfnParameter(this, 'S3PrefixListId', {
      allowedPattern: '^pl-[0-9a-f]+$',
      type: 'String'
    });

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      natGateways: 0,
      maxAzs: 2,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC },
        { name: 'private', subnetType: ec2.SubnetType.PRIVATE_ISOLATED }
      ]
    });
    this.vpc.addGatewayEndpoint('S3Endpoint', {
      service: ec2.GatewayVpcEndpointAwsService.S3
    });

    this.runtimeBucket = new s3.Bucket(this, 'RuntimeBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN,
      versioned: true
    });
    this.databaseSecurityGroup = new ec2.SecurityGroup(
      this,
      'DatabaseSecurityGroup',
      {
        vpc: this.vpc,
        allowAllOutbound: false
      }
    );
    this.databaseSecurityGroup.addEgressRule(
      ec2.Peer.prefixList(s3PrefixListId.valueAsString),
      ec2.Port.tcp(443),
      'Aurora S3 import only'
    );
    this.cluster = new rds.DatabaseCluster(this, 'Database', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_13
      }),
      writer: rds.ClusterInstance.serverlessV2('Writer', {
        publiclyAccessible: false
      }),
      credentials: rds.Credentials.fromGeneratedSecret('work_retrieval'),
      defaultDatabaseName: 'work_retrieval',
      deletionProtection: true,
      enableDataApi: true,
      serverlessV2AutoPauseDuration: Duration.minutes(10),
      serverlessV2MaxCapacity: 4,
      serverlessV2MinCapacity: 0,
      storageEncrypted: true,
      removalPolicy: RemovalPolicy.RETAIN,
      s3ImportBuckets: [this.runtimeBucket],
      securityGroups: [this.databaseSecurityGroup],
      vpc: this.vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED }
    });

    new CfnOutput(this, 'DatabaseClusterArn', {
      value: this.cluster.clusterArn
    });
    new CfnOutput(this, 'DatabaseSecretArn', {
      value: this.cluster.secret!.secretArn
    });
    new CfnOutput(this, 'RuntimeBucketName', {
      value: this.runtimeBucket.bucketName
    });
  }
}
