import {
  Aws,
  CfnCondition,
  CfnOutput,
  CfnParameter,
  DefaultStackSynthesizer,
  Duration,
  Fn,
  RemovalPolicy,
  Stack,
  Token,
  type StackProps
} from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cloudwatchActions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import type { Construct } from 'constructs';

const EMBEDDING_ENDPOINT_NAME = 'qwen3-embedding-8b-20260801-031826';
const EMBEDDING_ENDPOINT_CONFIG_NAME = EMBEDDING_ENDPOINT_NAME;
const EMBEDDING_MODEL_NAME = EMBEDDING_ENDPOINT_NAME;
const RERANKER_ENDPOINT_NAME = 'work-retrieval-qwen3-reranker-8b';
const GITHUB_PRODUCTION_SUBJECT =
  'repo:TakalaWang@50894789/1111-work-retrieval@1318865130:environment:production';

export interface PlatformStackProps extends StackProps {
  readonly apiRepository: ecr.IRepository;
  readonly cluster: rds.DatabaseCluster;
  readonly databaseSecurityGroup: ec2.ISecurityGroup;
  readonly runtimeBucket: s3.IBucket;
  readonly vpc: ec2.IVpc;
}

export class PlatformStack extends Stack {
  constructor(scope: Construct, id: string, props: PlatformStackProps) {
    super(scope, id, props);
    const {
      apiRepository,
      cluster,
      databaseSecurityGroup,
      runtimeBucket,
      vpc
    } = props;

    const apiImageUri = new CfnParameter(this, 'ApiImageUri', {
      type: 'String',
      allowedPattern:
        '^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$',
      description: 'Immutable API container image URI, including digest.'
    });
    const artifactManifestSha256 = new CfnParameter(
      this,
      'ArtifactManifestSha256',
      {
        type: 'String',
        allowedPattern: '^[a-f0-9]{64}$',
        description:
          'SHA-256 identifying runtime artifacts under runtime/<sha256>/.'
      }
    );
    const computeProfile = new CfnParameter(this, 'ComputeProfile', {
      type: 'String',
      default: 'cpu-incumbent',
      allowedValues: ['cpu-incumbent', 'gpu-shadow'],
      description:
        'Explicit serving compute profile; cpu-incumbent is the promoted BM25 hot path.'
    });
    const cpuIncumbentProfile = new CfnCondition(this, 'CpuIncumbentProfile', {
      expression: Fn.conditionEquals(
        computeProfile.valueAsString,
        'cpu-incumbent'
      )
    });
    const cpuServiceDesiredCount = Token.asNumber(
      Fn.conditionIf(cpuIncumbentProfile.logicalId, 1, 0)
    );
    const gpuMinCapacity = Token.asNumber(
      Fn.conditionIf(cpuIncumbentProfile.logicalId, '0', '1')
    );
    const gpuMaxCapacity = Token.asNumber(
      Fn.conditionIf(cpuIncumbentProfile.logicalId, '0', '2')
    );
    const gpuServiceDesiredCount = Token.asNumber(
      Fn.conditionIf(cpuIncumbentProfile.logicalId, 0, 1)
    );
    const gpuInstanceType = new CfnParameter(this, 'GpuInstanceType', {
      type: 'String',
      allowedPattern: '^[a-z0-9.]+$',
      description: 'EC2 GPU instance type, for example g5.xlarge.'
    });
    const alarmEmail = new CfnParameter(this, 'AlarmEmail', {
      type: 'String',
      default: '',
      allowedPattern: '^$|^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$',
      description:
        'Optional email for operational alarms; AWS requires subscription confirmation.'
    });
    for (const [name, service] of [
      ['EcrApiEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR],
      ['EcrDockerEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER],
      ['LogsEndpoint', ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
      ['SecretsEndpoint', ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER],
      [
        'SageMakerRuntimeEndpoint',
        ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_RUNTIME
      ]
    ] as const) {
      new ec2.InterfaceVpcEndpoint(this, name, {
        vpc,
        service,
        privateDnsEnabled: true
      });
    }
    new ec2.InterfaceVpcEndpoint(this, 'SageMakerApiEndpoint', {
      vpc,
      service: ec2.InterfaceVpcEndpointAwsService.SAGEMAKER_API,
      privateDnsEnabled: true
    });
    for (const [name, service] of [
      ['EcsEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS],
      ['EcsAgentEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS_AGENT],
      ['EcsTelemetryEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY]
    ] as const) {
      new ec2.InterfaceVpcEndpoint(this, name, {
        vpc,
        service,
        privateDnsEnabled: true
      });
    }

    const webBucket = new s3.Bucket(this, 'WebBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.RETAIN
    });

    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc
    });
    new ec2.CfnSecurityGroupIngress(this, 'EcsToDatabase', {
      description: 'API tasks only',
      fromPort: 5432,
      groupId: databaseSecurityGroup.securityGroupId,
      ipProtocol: 'tcp',
      sourceSecurityGroupId: ecsSecurityGroup.securityGroupId,
      toPort: 5432
    });
    const ecsCluster = new ecs.Cluster(this, 'EcsCluster', {
      containerInsightsV2: ecs.ContainerInsights.ENABLED,
      vpc
    });

    const autoScalingGroup = new autoscaling.AutoScalingGroup(
      this,
      'GpuAutoScalingGroup',
      {
        blockDevices: [
          {
            deviceName: '/dev/xvda',
            volume: autoscaling.BlockDeviceVolume.ebs(100, {
              encrypted: true,
              volumeType: autoscaling.EbsDeviceVolumeType.GP3
            })
          }
        ],
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
        instanceType: new ec2.InstanceType(gpuInstanceType.valueAsString),
        machineImage: ecs.EcsOptimizedImage.amazonLinux2023(
          ecs.AmiHardwareType.GPU
        ),
        minCapacity: gpuMinCapacity,
        maxCapacity: gpuMaxCapacity
      }
    );
    const capacityProvider = new ecs.AsgCapacityProvider(
      this,
      'GpuCapacityProvider',
      {
        autoScalingGroup,
        enableManagedTerminationProtection: false
      }
    );
    ecsCluster.addAsgCapacityProvider(capacityProvider);

    const logGroup = new logs.LogGroup(this, 'ApiLogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.RETAIN
    });
    const commonEnvironment = {
      ARTIFACT_BUCKET: runtimeBucket.bucketName,
      ARTIFACT_MANIFEST_SHA256: artifactManifestSha256.valueAsString,
      AWS_REGION: Aws.REGION,
      DB_HOST: cluster.clusterEndpoint.hostname,
      DB_NAME: 'work_retrieval',
      DB_PORT: cluster.clusterEndpoint.port.toString(),
      EMBEDDING_ENDPOINT_NAME,
      EMBEDDING_ENDPOINT_CONFIG_NAME,
      EMBEDDING_MODEL_NAME,
      RERANKER_ENDPOINT_NAME,
      SEARCH_ENABLE_DENSE_SHADOW: 'false',
      SEARCH_ENABLE_MULTIVIEW_MAXSIM: 'false',
      SEARCH_PORT_FACTORY:
        'work_retrieval_api.production:create_production_ports',
      SEARCH_RUNTIME_MANIFEST_PATH: '/tmp/work-retrieval-runtime/manifest.json',
      SEARCH_RUNTIME_ROOT: '/tmp/work-retrieval-runtime'
    };
    const databaseSecrets = {
      DB_PASSWORD: ecs.Secret.fromSecretsManager(cluster.secret!, 'password'),
      DB_USER: ecs.Secret.fromSecretsManager(cluster.secret!, 'username')
    };

    const cpuTaskDefinition = new ecs.FargateTaskDefinition(
      this,
      'CpuApiTaskDefinition',
      {
        cpu: 2048,
        memoryLimitMiB: 16384,
        runtimePlatform: {
          cpuArchitecture: ecs.CpuArchitecture.X86_64,
          operatingSystemFamily: ecs.OperatingSystemFamily.LINUX
        }
      }
    );
    grantApiRuntimeAccess(
      this,
      cpuTaskDefinition,
      apiRepository,
      runtimeBucket,
      artifactManifestSha256.valueAsString
    );
    const cpuContainer = cpuTaskDefinition.addContainer('Api', {
      image: ecs.ContainerImage.fromRegistry(apiImageUri.valueAsString),
      environment: commonEnvironment,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'cpu-api' }),
      secrets: databaseSecrets
    });
    cpuContainer.addPortMappings({ containerPort: 8000 });
    const cpuService = new ecs.FargateService(this, 'CpuApiService', {
      assignPublicIp: false,
      circuitBreaker: { rollback: true },
      cluster: ecsCluster,
      desiredCount: cpuServiceDesiredCount,
      healthCheckGracePeriod: Duration.minutes(10),
      minHealthyPercent: 100,
      securityGroups: [ecsSecurityGroup],
      taskDefinition: cpuTaskDefinition,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED }
    });

    const gpuTaskDefinition = new ecs.Ec2TaskDefinition(
      this,
      'GpuApiTaskDefinition',
      {
        networkMode: ecs.NetworkMode.AWS_VPC
      }
    );
    grantApiRuntimeAccess(
      this,
      gpuTaskDefinition,
      apiRepository,
      runtimeBucket,
      artifactManifestSha256.valueAsString
    );
    const gpuContainer = gpuTaskDefinition.addContainer('Api', {
      image: ecs.ContainerImage.fromRegistry(apiImageUri.valueAsString),
      environment: {
        ...commonEnvironment,
        NVIDIA_DRIVER_CAPABILITIES: 'compute,utility'
      },
      gpuCount: 1,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'gpu-api' }),
      memoryReservationMiB: 12288,
      secrets: databaseSecrets
    });
    gpuContainer.addPortMappings({ containerPort: 8000 });
    const gpuService = new ecs.Ec2Service(this, 'GpuApiService', {
      circuitBreaker: { rollback: true },
      cluster: ecsCluster,
      desiredCount: gpuServiceDesiredCount,
      healthCheckGracePeriod: Duration.minutes(10),
      minHealthyPercent: 100,
      securityGroups: [ecsSecurityGroup],
      taskDefinition: gpuTaskDefinition,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED }
    });

    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc
    });
    const cloudFrontPrefixList = new cr.AwsCustomResource(
      this,
      'CloudFrontPrefixList',
      {
        installLatestAwsSdk: false,
        onCreate: cloudFrontPrefixListLookup(),
        onUpdate: cloudFrontPrefixListLookup(),
        policy: cr.AwsCustomResourcePolicy.fromSdkCalls({
          resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE
        })
      }
    );
    new ec2.CfnSecurityGroupIngress(this, 'CloudFrontToAlb', {
      groupId: albSecurityGroup.securityGroupId,
      ipProtocol: 'tcp',
      fromPort: 80,
      toPort: 80,
      sourcePrefixListId: cloudFrontPrefixList.getResponseField(
        'PrefixLists.0.PrefixListId'
      ),
      description: 'CloudFront origin-facing managed prefix list only'
    });
    const loadBalancer = new elbv2.ApplicationLoadBalancer(
      this,
      'ApiLoadBalancer',
      {
        internetFacing: true,
        securityGroup: albSecurityGroup,
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC }
      }
    );
    const listener = loadBalancer.addListener('HttpListener', {
      port: 80,
      open: false,
      defaultAction: elbv2.ListenerAction.fixedResponse(403)
    });
    const originSecret = new secretsmanager.Secret(
      this,
      'CloudFrontOriginSecret',
      {
        generateSecretString: { excludePunctuation: true, passwordLength: 64 }
      }
    );
    const targetGroup = listener.addTargets('ApiTargets', {
      conditions: [
        elbv2.ListenerCondition.httpHeader('X-Origin-Verify', [
          originSecret.secretValue.unsafeUnwrap()
        ])
      ],
      priority: 1,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [cpuService, gpuService],
      healthCheck: { path: '/readyz' }
    });

    const webAcl = new wafv2.CfnWebACL(this, 'ApiWebAcl', {
      scope: 'REGIONAL',
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: 'work-retrieval-api',
        sampledRequestsEnabled: true
      },
      rules: [
        {
          name: 'AWSManagedCommonRules',
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              name: 'AWSManagedRulesCommonRuleSet',
              vendorName: 'AWS'
            }
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'aws-managed-common-rules',
            sampledRequestsEnabled: true
          }
        },
        {
          name: 'IpRateLimit',
          priority: 1,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              aggregateKeyType: 'FORWARDED_IP',
              forwardedIpConfig: {
                fallbackBehavior: 'MATCH',
                headerName: 'X-Forwarded-For'
              },
              limit: 1000
            }
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'ip-rate-limit',
            sampledRequestsEnabled: true
          }
        }
      ]
    });
    new wafv2.CfnWebACLAssociation(this, 'ApiWebAclAssociation', {
      resourceArn: loadBalancer.loadBalancerArn,
      webAclArn: webAcl.attrArn
    });

    const target5xxAlarm = new cloudwatch.Alarm(this, 'Target5xxAlarm', {
      metric: targetGroup.metrics.httpCodeTarget(
        elbv2.HttpCodeTarget.TARGET_5XX_COUNT,
        { period: Duration.minutes(5), statistic: 'Sum' }
      ),
      threshold: 5,
      evaluationPeriods: 1,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
    });
    const unhealthyHostAlarm = new cloudwatch.Alarm(
      this,
      'UnhealthyHostAlarm',
      {
        metric: targetGroup.metrics.unhealthyHostCount({
          period: Duration.minutes(1),
          statistic: 'Maximum'
        }),
        threshold: 1,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING
      }
    );
    const alarmTopic = new sns.Topic(this, 'OperationalAlarmTopic');
    const alarmEmailConfigured = new CfnCondition(
      this,
      'AlarmEmailConfigured',
      {
        expression: Fn.conditionNot(
          Fn.conditionEquals(alarmEmail.valueAsString, '')
        )
      }
    );
    const emailSubscription = new sns.CfnSubscription(
      this,
      'OperationalAlarmEmailSubscription',
      {
        endpoint: alarmEmail.valueAsString,
        protocol: 'email',
        topicArn: alarmTopic.topicArn
      }
    );
    emailSubscription.cfnOptions.condition = alarmEmailConfigured;
    const alarmAction = new cloudwatchActions.SnsAction(alarmTopic);
    target5xxAlarm.addAlarmAction(alarmAction);
    unhealthyHostAlarm.addAlarmAction(alarmAction);

    const apiOrigin = new origins.LoadBalancerV2Origin(loadBalancer, {
      customHeaders: {
        'X-Origin-Verify': originSecret.secretValue.unsafeUnwrap()
      },
      protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
      httpPort: 80
    });
    const apiBehavior: cloudfront.BehaviorOptions = {
      origin: apiOrigin,
      allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
      originRequestPolicy:
        cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY
    };
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(webBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
      },
      additionalBehaviors: {
        '/api/*': apiBehavior,
        '/healthz': apiBehavior,
        '/readyz': apiBehavior
      }
    });

    const githubProvider = new iam.OpenIdConnectProvider(
      this,
      'GitHubProvider',
      {
        url: 'https://token.actions.githubusercontent.com',
        clientIds: ['sts.amazonaws.com']
      }
    );
    const githubRole = new iam.Role(this, 'GitHubDeployRole', {
      assumedBy: new iam.WebIdentityPrincipal(
        githubProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
            'token.actions.githubusercontent.com:sub': GITHUB_PRODUCTION_SUBJECT
          }
        }
      )
    });
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['sts:AssumeRole'],
        resources: [
          'deploy-role',
          'file-publishing-role',
          'image-publishing-role',
          'lookup-role'
        ].map(
          (role) =>
            `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role/cdk-${DefaultStackSynthesizer.DEFAULT_QUALIFIER}-${role}-${Aws.ACCOUNT_ID}-${Aws.REGION}`
        )
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:CompleteLayerUpload',
          'ecr:DescribeImages',
          'ecr:DescribeImageScanFindings',
          'ecr:InitiateLayerUpload',
          'ecr:PutImage',
          'ecr:UploadLayerPart'
        ],
        resources: [apiRepository.repositoryArn]
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*']
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['ec2:DescribeManagedPrefixLists'],
        resources: ['*']
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [runtimeBucket.arnForObjects('runtime/*/manifest.json')]
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetBucketLocation', 's3:ListBucket'],
        resources: [webBucket.bucketArn]
      })
    );
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:DeleteObject', 's3:GetObject', 's3:PutObject'],
        resources: [webBucket.arnForObjects('*')]
      })
    );
    distribution.grantCreateInvalidation(githubRole);
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['cloudfront:GetInvalidation'],
        resources: [distribution.distributionArn]
      })
    );

    new CfnOutput(this, 'ApiRepositoryUri', {
      value: apiRepository.repositoryUri
    });
    new CfnOutput(this, 'ApiBaseUrl', {
      value: `https://${distribution.domainName}/api/v1`
    });
    new CfnOutput(this, 'WebUrl', {
      value: `https://${distribution.domainName}`
    });
    new CfnOutput(this, 'DistributionDomainName', {
      value: distribution.domainName
    });
    new CfnOutput(this, 'DistributionId', {
      value: distribution.distributionId
    });
    new CfnOutput(this, 'AlbDnsName', {
      value: loadBalancer.loadBalancerDnsName
    });
    new CfnOutput(this, 'AlbArn', {
      value: loadBalancer.loadBalancerArn
    });
    new CfnOutput(this, 'ApiTargetGroupArn', {
      value: targetGroup.targetGroupArn
    });
    new CfnOutput(this, 'EcsClusterName', { value: ecsCluster.clusterName });
    new CfnOutput(this, 'EcsClusterArn', { value: ecsCluster.clusterArn });
    new CfnOutput(this, 'CpuServiceName', { value: cpuService.serviceName });
    new CfnOutput(this, 'CpuServiceArn', { value: cpuService.serviceArn });
    new CfnOutput(this, 'GpuServiceName', { value: gpuService.serviceName });
    new CfnOutput(this, 'GpuServiceArn', { value: gpuService.serviceArn });
    new CfnOutput(this, 'GpuAutoScalingGroupName', {
      value: autoScalingGroup.autoScalingGroupName
    });
    new CfnOutput(this, 'GpuAutoScalingGroupArn', {
      value: autoScalingGroup.autoScalingGroupArn
    });
    new CfnOutput(this, 'WebAclArn', { value: webAcl.attrArn });
    new CfnOutput(this, 'Target5xxAlarmName', {
      value: target5xxAlarm.alarmName
    });
    new CfnOutput(this, 'UnhealthyHostAlarmName', {
      value: unhealthyHostAlarm.alarmName
    });
    new CfnOutput(this, 'OperationalAlarmTopicArn', {
      value: alarmTopic.topicArn
    });
    new CfnOutput(this, 'GitHubDeployRoleArn', { value: githubRole.roleArn });
    new CfnOutput(this, 'WebBucketName', { value: webBucket.bucketName });
  }
}

function grantApiRuntimeAccess(
  stack: Stack,
  taskDefinition: ecs.TaskDefinition,
  apiRepository: ecr.IRepository,
  runtimeBucket: s3.IBucket,
  artifactManifestSha256: string
): void {
  apiRepository.grantPull(taskDefinition.obtainExecutionRole());
  const runtimePrefix = `runtime/${artifactManifestSha256}/`;
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['s3:GetObject', 's3:GetObjectVersion'],
      resources: [runtimeBucket.arnForObjects(`${runtimePrefix}*`)]
    })
  );
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['s3:ListBucket'],
      conditions: { StringLike: { 's3:prefix': [`${runtimePrefix}*`] } },
      resources: [runtimeBucket.bucketArn]
    })
  );
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['sagemaker:InvokeEndpoint'],
      resources: [
        sagemakerEndpointArn(stack, EMBEDDING_ENDPOINT_NAME),
        sagemakerEndpointArn(stack, RERANKER_ENDPOINT_NAME)
      ]
    })
  );
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['sagemaker:DescribeEndpoint'],
      resources: [sagemakerEndpointArn(stack, EMBEDDING_ENDPOINT_NAME)]
    })
  );
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['sagemaker:DescribeEndpointConfig'],
      resources: [
        stack.formatArn({
          service: 'sagemaker',
          resource: 'endpoint-config',
          resourceName: EMBEDDING_ENDPOINT_CONFIG_NAME
        })
      ]
    })
  );
  taskDefinition.addToTaskRolePolicy(
    new iam.PolicyStatement({
      actions: ['sagemaker:DescribeModel'],
      resources: [
        stack.formatArn({
          service: 'sagemaker',
          resource: 'model',
          resourceName: EMBEDDING_MODEL_NAME
        })
      ]
    })
  );
}

function sagemakerEndpointArn(stack: Stack, endpointName: string): string {
  return stack.formatArn({
    service: 'sagemaker',
    resource: 'endpoint',
    resourceName: endpointName
  });
}

function cloudFrontPrefixListLookup(): cr.AwsSdkCall {
  return {
    service: 'EC2',
    action: 'describeManagedPrefixLists',
    parameters: {
      Filters: [
        {
          Name: 'prefix-list-name',
          Values: ['com.amazonaws.global.cloudfront.origin-facing']
        }
      ]
    },
    physicalResourceId: cr.PhysicalResourceId.of(
      'cloudfront-origin-facing-prefix-list'
    )
  };
}
