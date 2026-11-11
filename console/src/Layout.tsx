import { NavLink, Outlet } from "react-router-dom";

interface LayoutProps {
  onSignOut: () => void;
}

export function Layout({ onSignOut }: LayoutProps) {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">BeeBox</span>
          <span className="brand-subtitle">控制台</span>
        </div>
        <nav className="nav">
          <NavLink to="/jobs" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            任务
          </NavLink>
          <NavLink
            to="/submit"
            data-testid="nav-submit"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
          >
            提交任务
          </NavLink>
          <NavLink to="/models" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            模型
          </NavLink>
          <NavLink
            to="/credentials"
            data-testid="nav-credentials"
            className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}
          >
            平台凭证
          </NavLink>
          <NavLink to="/health" className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
            健康与队列
          </NavLink>
        </nav>
        <button type="button" className="button ghost" data-testid="logout-button" onClick={onSignOut}>
          退出登录
        </button>
      </header>
      <main className="content">
        <Outlet />
      </main>
      <footer className="footer">所有操作只通过公开 /v1 接口 · 控制台不做字幕编辑</footer>
    </div>
  );
}
