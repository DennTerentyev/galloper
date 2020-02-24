#   Copyright 2019 getcarrier.io
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

from sqlalchemy import or_
from flask import Blueprint, request, render_template
from flask_restful import Api, Resource, reqparse
from galloper.models.api_reports import APIReport
from galloper.models.security_results import SecurityResults
from galloper.models.security_reports import SecurityReport
from galloper.data_utils.report_utils import render_analytics_control
from galloper.data_utils.charts_utils import (requests_summary, requests_hits, avg_responses, summary_table,
                                              get_issues, get_data_from_influx, prepare_comparison_responses,
                                              compare_tests, create_benchmark_dataset)

from galloper.dal.influx_results import (get_test_details, get_sampler_types, delete_test_data)

bp = Blueprint('reports', __name__)
api = Api(bp)


@bp.route("/report", methods=["GET"])
def report():
    return render_template('perftemplate/report.html')


@bp.route("/security", methods=["GET"])
def security():
    return render_template('security/report.html')


@bp.route('/report/backend', methods=["GET", "POST"])
def view_report():
    if request.method == 'GET':
        if request.args.get("report_id", None):
            test_data = APIReport.query.filter_by(id=request.args.get("report_id")).first().to_json()
        else:
            test_data = get_test_details(build_id=request.args['build_id'], test_name=request.args['test_name'],
                                         lg_type=request.args['lg_type'])
        analytics_control = render_analytics_control(test_data['requests'])
        samplers = get_sampler_types(test_data['build_id'], test_data['name'], test_data['lg_type'])
        return render_template('perftemplate/api_test_report.html', test_data=test_data,
                               analytics_control=analytics_control, samplers=samplers)


@bp.route("/report/compare", methods=["GET"])
def compare_reports():
    samplers = set()
    requests_data = set()
    tests = request.args.getlist('id[]')
    for each in APIReport.query.filter(APIReport.id.in_(tests)).order_by(APIReport.id.asc()).all():
        samplers.update(get_sampler_types(each.build_id, each.name, each.lg_type))
        requests_data.update(set(each.requests.split(";")))
    return render_template('perftemplate/comparison_report.html', samplers=samplers, requests=requests_data)


get_report_parser = reqparse.RequestParser()
get_report_parser.add_argument('offset', type=int, default=0, location="args")
get_report_parser.add_argument('limit', type=int, default=0, location="args")
get_report_parser.add_argument('search', type=str, default='', location="args")
get_report_parser.add_argument('sort', type=str, default='', location="args")
get_report_parser.add_argument('order', type=str, default='', location="args")

delete_report_parser = reqparse.RequestParser()
delete_report_parser.add_argument('id[]', type=list, action='append', location="args")


class ReportApi(Resource):
    _parser = reqparse.RequestParser()
    _parser.add_argument("build_id", type=str, location="json")
    _parser.add_argument("test_name", type=str, location="json")
    _parser.add_argument("lg_type", type=str, location="json")

    put_parser = _parser.copy()
    put_parser.add_argument("missed", type=int, location="json")

    port_parser = put_parser.copy()
    put_parser.add_argument("start_time", type=str, location="json")
    put_parser.add_argument("duration", type=float, location="json")
    put_parser.add_argument("vusers", type=int, location="json")
    put_parser.add_argument("name", type=str, location="json")
    put_parser.add_argument("environment", type=str, location="json")
    put_parser.add_argument("type", type=str, location="json")


    def get(self):
        reports = []
        args = get_report_parser.parse_args(strict=False)
        if args.get('sort'):
            sort_rule = getattr(getattr(APIReport, args["sort"]), args["sort_order"])()
        else:
            sort_rule = APIReport.id.asc()
        if not args.get('search') and not args.get('sort'):
            total = APIReport.query.order_by(sort_rule).count()
            res = APIReport.query.order_by(sort_rule).limit(args.get('limit')).offset(args.get('offset')).all()
        else:
            filter_ = or_(APIReport.name.like(f'%{args["search"]}%'),
                          APIReport.environment.like(f'%{args["search"]}%'),
                          APIReport.type.like(f'%{args["search"]}%'))
            res = APIReport.query.filter(filter_).order_by(sort_rule).\
                limit(args.get('limit')).offset(args.get('offset')).all()
            total = APIReport.query.order_by(sort_rule).filter(filter_).count()
        for each in res:
            each_json = each.to_json()
            each_json['start_time'] = each_json['start_time'].replace("T", " ").split(".")[0]
            each_json['duration'] = int(each_json['duration'])
            each_json['failure_rate'] = round((each_json['failures'] / each_json['total']) * 100, 2)
            reports.append(each_json)
        return {"total": total, "rows": reports}

    def post(self):
        args = self.post_parser.parse_args(strict=False)
        report = APIReport(name=args['name'], environment=args["environment"], type=args["type"],
                           end_time='', start_time=args["start_time"], failures=0,
                           total=0, thresholds_missed=0, throughput=0, vusers=args["vusers"],
                           pct95=0, duration=args["duration"], build_id=args['build_id'],
                           lg_type=args['lg_type'], onexx=0, twoxx=0, threexx=0,
                           fourxx=0, fivexx=0, requests="")
        report.insert()
        return {"message": "created"}

    def put(self):
        args = self.put_parser.parse_args(strict=False)
        test_data = get_test_details(build_id=args['build_id'], test_name=args['test_name'], lg_type=args['lg_type'])
        report = APIReport.query.filter_by(build_id=request.args['build_id']).first()
        report.end_time = test_data["end_time"]
        report.start_time = test_data["start_time"]
        report.failures = test_data["failures"]
        report.total = test_data["total"]
        report.thresholds_missed = args.get("missed", 0)
        report.throughput = test_data["throughput"]
        report.pct95 = test_data["pct95"]
        report.onexx = test_data["onexx"]
        report.twoxx = test_data["twoxx"]
        report.threexx = test_data["threexx"]
        report.fourxx = test_data["fourxx"]
        report.fivexx = test_data["fivexx"]
        report.requests = ";".join(test_data["requests"])
        report.commit()
        return {"message": "updated"}

    def delete(self):
        args = delete_report_parser.parse_args(strict=False)
        for each in APIReport.query.filter(APIReport.id.in_(args["id[]"])).order_by(APIReport.id.asc()).all():
            delete_test_data(each.build_id, each.name, each.lg_type)
            each.delete()
        return {"message": "deleted"}


reports_parser = reqparse.RequestParser()
reports_parser.add_argument("low_value", type=float, default=0, location="args")
reports_parser.add_argument("high_value", type=float, default=100, location="args")
reports_parser.add_argument("start_time", type=str, default='', location="args")
reports_parser.add_argument("end_time", type=str, default='', location="args")
reports_parser.add_argument("aggregator", type=str, default="auto", location="args")
reports_parser.add_argument("sampler", type=str, default='REQUEST', location="args")
reports_parser.add_argument("metric", type=str, default='', location="args")
reports_parser.add_argument("scope", type=str, default='', location="args")


class ReportChartsApi(Resource):
    get_parser = reports_parser.copy()
    get_parser.add_argument("build_id", type=str, location="args")
    get_parser.add_argument("test_name", type=str, location="args")
    get_parser.add_argument("lg_type", type=str, location="args")

    mapping = {
        "requests": {
            "summary": requests_summary,
            "hits": requests_hits,
            "average": avg_responses,
            "table": summary_table,
            "data": get_data_from_influx
        },
        "errors": {
            "table": get_issues
        }

    }

    def get(self, source, target):
        args = self.get_parser.parse_args(strict=False)
        return self.mapping[source][target](args)


class ReportsCompareApi(Resource):
    get_parser = reports_parser.copy()
    get_parser.add_argument('id[]', action='append', location="args")
    get_parser.add_argument("request", type=str, default='', location="args")
    get_parser.add_argument("calculation", type=str, default='', location="args")

    mapping = {
        "data": prepare_comparison_responses,
        "tests": compare_tests,
        "benchmark": create_benchmark_dataset
    }

    def get(self, target):
        args = self.get_parser.parse_args(strict=False)
        return self.mapping[target](args)


class SecurityReportApi(Resource):
    def get(self):
        reports = []
        args = get_report_parser.parse_args(strict=False)
        if args.get('sort'):
            sort_rule = getattr(getattr(SecurityResults, args["sort"]), args["sort_order"])()
        else:
            sort_rule = SecurityResults.id.asc()
        if not args.get('search') and not args.get('sort'):
            total = SecurityResults.query.order_by(sort_rule).count()
            res = SecurityResults.query.order_by(sort_rule).limit(args.get('limit')).offset(args.get('offset')).all()
        else:
            filter_ = or_(SecurityResults.project_name.like(f'%{args["search"]}%'),
                          SecurityResults.app_name.like(f'%{args["search"]}%'),
                          SecurityResults.scan_type.like(f'%{args["search"]}%'),
                          SecurityResults.environment.like(f'%{args["search"]}%'),
                          SecurityResults.endpoint.like(f'%{args["search"]}%'))
            res = SecurityResults.query.filter(filter_).order_by(sort_rule). \
                limit(args.get('limit')).offset(args.get('offset')).all()
            total = SecurityResults.query.order_by(sort_rule).filter(filter_).count()
        for each in res:
            each_json = each.to_json()
            each_json['scan_time'] = each_json['start_time'].replace("T", " ").split(".")[0]
            each_json['duration'] = int(each_json['duration'])
            reports.append(each_json)
        return {"total": total, "rows": reports}

    def delete(self):
        args = delete_report_parser.parse_args(strict=False)
        for each in SecurityReport.query.filter(SecurityReport.report_id.in_(args["id[]"])
                                                 ).order_by(SecurityReport.id.asc()).all():
            each.delete()
        for each in SecurityResults.query.filter(SecurityResults.id.in_(args["id[]"])
                                                 ).order_by(SecurityResults.id.asc()).all():
            delete_test_data(each.build_id, each.name, each.lg_type)
            each.delete()
        return {"message": "deleted"}


api.add_resource(ReportApi, "/api/report")
api.add_resource(ReportChartsApi, "/api/chart/<source>/<target>")
api.add_resource(ReportsCompareApi, "/api/compare/<target>")
api.add_resource(SecurityReportApi, "/api/security")





