import { AGENT_ROLES, type Methodology } from "../src/contracts.js";

/** 测试用方法论载荷。生产环境由 engine/methodology.py 装配后随请求下发。 */
export const methodology: Methodology = {
  text: "情绪周期七阶段：启动/发酵/高潮/分歧/退潮/冰点/修复。均线与量价共振才确认，资金面须独立确认，口径为 1~5 日短线。不得编造未出现在输入里的事实。",
  role_briefs: Object.fromEntries(AGENT_ROLES.map((role) => [role, `${role} 角色职责说明`])),
};
