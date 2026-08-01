import {
  Aws,
  CfnOutput,
  CfnParameter,
  RemovalPolicy,
  Stack,
  type StackProps
} from 'aws-cdk-lib';
import * as autoscaling from 'aws-cdk-lib/aws-autoscaling';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import type { Construct } from 'constructs';

export interface PlatformStackProps extends StackProps {
  readonly cluster: rds.DatabaseCluster;
  readonly databaseSecurityGroup: ec2.ISecurityGroup;
  readonly runtimeBucket: s3.IBucket;
  readonly vpc: ec2.IVpc;
}

export class PlatformStack extends Stack {
  constructor(scope: Construct, id: string, props: PlatformStackProps) {
    super(scope, id, props);
    const { cluster, databaseSecurityGroup, runtimeBucket, vpc } = props;

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
    const gpuInstanceType = new CfnParameter(this, 'GpuInstanceType', {
      type: 'String',
      allowedPattern: '^[a-z0-9.]+$',
      description: 'EC2 GPU instance type, for example g5.xlarge.'
    });
    const gpuMinCapacity = capacityParameter(this, 'GpuMinCapacity');
    const gpuMaxCapacity = capacityParameter(this, 'GpuMaxCapacity');
    const gpuServiceDesiredCount = capacityParameter(
      this,
      'GpuServiceDesiredCount'
    );

    for (const [name, service] of [
      ['EcrApiEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR],
      ['EcrDockerEndpoint', ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER],
      ['EcsEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS],
      ['EcsAgentEndpoint', ec2.InterfaceVpcEndpointAwsService.ECS_AGENT],
      [
        'EcsTelemetryEndpoint',
        ec2.InterfaceVpcEndpointAwsService.ECS_TELEMETRY
      ],
      ['LogsEndpoint', ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
      ['SecretsEndpoint', ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER]
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
    const repository = new ecr.Repository(this, 'ApiRepository', {
      encryption: ecr.RepositoryEncryption.AES_256,
      imageScanOnPush: true,
      removalPolicy: RemovalPolicy.RETAIN,
      lifecycleRules: [{ maxImageCount: 20 }]
    });

    const ecsSecurityGroup = new ec2.SecurityGroup(this, 'EcsSecurityGroup', {
      vpc
    });
    new ec2.CfnSecurityGroupIngress(this, 'EcsToDatabase', {
      description: 'ECS tasks only',
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
        vpc,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
        instanceType: new ec2.InstanceType(gpuInstanceType.valueAsString),
        machineImage: ecs.EcsOptimizedImage.amazonLinux2(
          ecs.AmiHardwareType.GPU
        ),
        minCapacity: gpuMinCapacity.valueAsNumber,
        maxCapacity: gpuMaxCapacity.valueAsNumber
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

    const taskDefinition = new ecs.Ec2TaskDefinition(
      this,
      'ApiTaskDefinition',
      {
        networkMode: ecs.NetworkMode.AWS_VPC
      }
    );
    runtimeBucket.grantRead(
      taskDefinition.taskRole,
      `runtime/${artifactManifestSha256.valueAsString}/*`
    );
    cluster.secret?.grantRead(taskDefinition.taskRole);
    taskDefinition.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: ['ecr:GetAuthorizationToken'],
        resources: ['*']
      })
    );
    taskDefinition.addToExecutionRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage'
        ],
        resources: [
          `arn:${Aws.PARTITION}:ecr:${Aws.REGION}:${Aws.ACCOUNT_ID}:repository/*`
        ]
      })
    );
    const logGroup = new logs.LogGroup(this, 'ApiLogGroup', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: RemovalPolicy.RETAIN
    });
    const container = taskDefinition.addContainer('Api', {
      image: ecs.ContainerImage.fromRegistry(apiImageUri.valueAsString),
      environment: {
        ARTIFACT_BUCKET: runtimeBucket.bucketName,
        ARTIFACT_MANIFEST_SHA256: artifactManifestSha256.valueAsString,
        DATABASE_CLUSTER_ARN: cluster.clusterArn,
        DATABASE_NAME: 'work_retrieval',
        DATABASE_SECRET_ARN: cluster.secret!.secretArn
      },
      gpuCount: 1,
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'api' }),
      memoryReservationMiB: 4096
    });
    container.addPortMappings({ containerPort: 8000 });
    const service = new ecs.Ec2Service(this, 'ApiService', {
      cluster: ecsCluster,
      taskDefinition,
      desiredCount: gpuServiceDesiredCount.valueAsNumber,
      capacityProviderStrategies: [
        { capacityProvider: capacityProvider.capacityProviderName, weight: 1 }
      ],
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
      securityGroups: [ecsSecurityGroup],
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
    listener.addTargets('ApiTargets', {
      conditions: [
        elbv2.ListenerCondition.httpHeader('X-Origin-Verify', [
          originSecret.secretValue.unsafeUnwrap()
        ])
      ],
      priority: 1,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
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
        }
      ]
    });
    new wafv2.CfnWebACLAssociation(this, 'ApiWebAclAssociation', {
      resourceArn: loadBalancer.loadBalancerArn,
      webAclArn: webAcl.attrArn
    });

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

    const githubProvider =
      iam.OpenIdConnectProvider.fromOpenIdConnectProviderArn(
        this,
        'GitHubProvider',
        `arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com`
      );
    const githubRole = new iam.Role(this, 'GitHubDeployRole', {
      assumedBy: new iam.WebIdentityPrincipal(
        githubProvider.openIdConnectProviderArn,
        {
          StringEquals: {
            'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
            'token.actions.githubusercontent.com:sub':
              'repo:TakalaWang/1111-work-retrieval:environment:production'
          }
        }
      )
    });
    githubRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['sts:AssumeRole'],
        resources: [`arn:${Aws.PARTITION}:iam::${Aws.ACCOUNT_ID}:role/cdk-*`]
      })
    );
    webBucket.grantReadWrite(githubRole);
    distribution.grantCreateInvalidation(githubRole);

    new CfnOutput(this, 'ApiRepositoryUri', {
      value: repository.repositoryUri
    });
    new CfnOutput(this, 'DistributionDomainName', {
      value: distribution.domainName
    });
    new CfnOutput(this, 'DistributionId', {
      value: distribution.distributionId
    });
    new CfnOutput(this, 'GitHubDeployRoleArn', { value: githubRole.roleArn });
    new CfnOutput(this, 'WebBucketName', { value: webBucket.bucketName });
  }
}

function capacityParameter(stack: Stack, id: string): CfnParameter {
  return new CfnParameter(stack, id, {
    type: 'Number',
    default: 0,
    minValue: 0
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
