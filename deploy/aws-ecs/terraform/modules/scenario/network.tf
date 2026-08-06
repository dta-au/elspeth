data "aws_availability_zones" "available" {
  state = "available"

  # Standard availability zones only: with Local Zones opted in, the
  # unfiltered list can select zones where Aurora, EFS, and internet-facing
  # ALBs are unsupported, and these subnets host all three.
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

resource "aws_vpc" "scenario" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.tags, { Name = "${local.namespace}-vpc" })
}

resource "aws_internet_gateway" "scenario" {
  vpc_id = aws_vpc.scenario.id

  tags = merge(local.tags, { Name = "${local.namespace}-igw" })
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.scenario.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = merge(local.tags, { Name = "${local.namespace}-public-${count.index + 1}" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.scenario.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.scenario.id
  }

  tags = merge(local.tags, { Name = "${local.namespace}-public" })
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "alb" {
  name        = "${local.namespace}-alb"
  description = "Disposable ELSPETH acceptance ALB"
  vpc_id      = aws_vpc.scenario.id

  tags = merge(local.tags, { Name = "${local.namespace}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.alb_https_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"

  tags = local.tags
}

resource "aws_security_group" "task" {
  name        = "${local.namespace}-task"
  description = "Disposable ELSPETH acceptance tasks"
  vpc_id      = aws_vpc.scenario.id

  tags = merge(local.tags, { Name = "${local.namespace}-task" })
}

resource "aws_vpc_security_group_ingress_rule" "task_from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8451
  to_port                      = 8451
  ip_protocol                  = "tcp"

  tags = local.tags
}

resource "aws_vpc_security_group_egress_rule" "alb_to_task" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 8451
  to_port                      = 8451
  ip_protocol                  = "tcp"

  tags = local.tags
}

resource "aws_vpc_security_group_egress_rule" "task_all" {
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"

  tags = local.tags
}

resource "aws_security_group" "database" {
  name        = "${local.namespace}-database"
  description = "Disposable ELSPETH acceptance Aurora"
  vpc_id      = aws_vpc.scenario.id

  tags = merge(local.tags, { Name = "${local.namespace}-database" })
}

resource "aws_vpc_security_group_ingress_rule" "database_from_task" {
  security_group_id            = aws_security_group.database.id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"

  tags = local.tags
}

resource "aws_security_group" "efs" {
  name        = "${local.namespace}-efs"
  description = "Disposable ELSPETH acceptance EFS"
  vpc_id      = aws_vpc.scenario.id

  tags = merge(local.tags, { Name = "${local.namespace}-efs" })
}

resource "aws_vpc_security_group_ingress_rule" "efs_from_task" {
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = aws_security_group.task.id
  from_port                    = 2049
  to_port                      = 2049
  ip_protocol                  = "tcp"

  tags = local.tags
}

resource "aws_lb" "web" {
  name                       = "${local.namespace}-alb"
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  idle_timeout               = var.alb_idle_timeout_seconds
  drop_invalid_header_fields = true
  enable_deletion_protection = false

  # An internet-facing ALB requires the VPC's internet gateway to be
  # attached; nothing else in this resource references the gateway, so
  # without the explicit edge a fresh apply can race the attachment.
  depends_on = [aws_internet_gateway.scenario]

  tags = merge(local.tags, { Name = "${local.namespace}-alb" })
}

resource "aws_lb_target_group" "web" {
  name        = "${local.namespace}-target"
  port        = 8451
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.scenario.id

  deregistration_delay = 30

  health_check {
    enabled             = true
    path                = "/api/ready"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 6
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = merge(local.tags, { Name = "${local.namespace}-target" })
}

resource "tls_private_key" "alb" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "alb" {
  private_key_pem       = tls_private_key.alb.private_key_pem
  validity_period_hours = 24
  early_renewal_hours   = 1
  dns_names             = [aws_lb.web.dns_name]
  allowed_uses          = ["key_encipherment", "digital_signature", "server_auth"]

  subject {
    organization = "ELSPETH disposable acceptance"
  }
}

resource "aws_acm_certificate" "alb" {
  private_key      = tls_private_key.alb.private_key_pem
  certificate_body = tls_self_signed_cert.alb.cert_pem

  tags = local.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.alb.arn

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "not found"
      status_code  = "404"
    }
  }

  tags = local.tags
}

resource "aws_lb_listener_rule" "traffic" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }

  tags = local.tags
}
